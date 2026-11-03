# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Context-aware ontology alignment pipeline.

Aligns concepts across multiple induced ontologies using:
  1. Name-based candidate discovery (same localName in 2+ repos)
  2. Graph neighborhood context (superclass, properties, children, siblings)
  3. LLM classification into SKOS relationship types
"""

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import rdflib

from coa_ontology.inducer.services.llm import LLMClient

logger = logging.getLogger(__name__)

CLASSIFY_SYSTEM = """You are an ontology alignment expert. \
Given two concept descriptions with their graph context, classify their relationship.

Output EXACTLY one line: RELATIONSHIP_TYPE|confidence|reason

RELATIONSHIP_TYPE is one of:
- exactMatch: same concept, same meaning
- closeMatch: very similar, nearly interchangeable
- broadMatch: first is broader than second
- narrowMatch: first is narrower than second
- relatedMatch: associated but distinct concepts
- noMatch: different concepts that happen to share a name

confidence: 0.0-1.0
reason: one sentence explanation"""


@dataclass
class AlignmentResult:
    """Outcome of classifying how a class relates across two repositories."""

    class_name: str
    relationship: str  # exactMatch, closeMatch, broadMatch, narrowMatch, relatedMatch, noMatch
    confidence: float
    reason: str
    repo_a: str
    repo_b: str


@dataclass
class AlignmentConfig:
    """Tuning parameters for a cross-repository alignment run."""

    min_repos: int = 2  # minimum repos a class must appear in
    workers: int = 4  # parallel LLM calls
    max_pairs: int | None = None  # limit pairs to classify (None = all)


class AlignmentPipeline:
    """Aligns cross-repo concepts using graph context + LLM classification."""

    def __init__(self, graph_client, llm: LLMClient):
        """Store the graph client and LLM used to align cross-repo concepts.

        Args:
            graph_client: object with a `query(cypher) -> list[dict]` method (NA or similar)
            llm: LLM client for classification
        """
        self.gc = graph_client
        self.llm = llm

    def find_candidates(self, config: AlignmentConfig) -> list[dict]:
        """Find class names appearing in min_repos+ repos."""
        results = self.gc.query(f"""
            MATCH (n:MultiClass)
            WITH n.localName AS name, collect(DISTINCT n.repo) AS repos
            WHERE size(repos) >= {config.min_repos}
            RETURN name, repos ORDER BY size(repos) DESC
        """)
        logger.info(f"Found {len(results)} candidate classes in {config.min_repos}+ repos")
        return results

    def gather_context(self, class_name: str) -> dict:
        """Collect graph neighborhood for a class."""
        instances = self.gc.query(
            f"MATCH (n:MultiClass {{localName: '{class_name}'}}) "
            f"RETURN n.source AS src, n.repo AS repo, n.comment AS comment, n.superclass AS sup LIMIT 10"
        )
        props = self.gc.query(
            f"MATCH (p:MultiProp {{domain: '{class_name}'}}) RETURN DISTINCT p.localName AS name LIMIT 8"
        )
        children = self.gc.query(
            f"MATCH (c:MultiClass {{superclass: '{class_name}'}}) RETURN DISTINCT c.localName AS name LIMIT 8"
        )
        sups = set(i.get("sup", "") for i in instances if i.get("sup"))
        siblings: list[str] = []
        for sup in sups:
            if sup:
                sibs = self.gc.query(
                    f"MATCH (s:MultiClass {{superclass: '{sup}'}}) "
                    f"WHERE s.localName <> '{class_name}' RETURN DISTINCT s.localName AS name LIMIT 5"
                )
                siblings.extend(s["name"] for s in sibs)

        return {
            "name": class_name,
            "instances": instances,
            "properties": [p["name"] for p in props],
            "children": [c["name"] for c in children],
            "siblings": list(set(siblings))[:10],
            "superclasses": list(sups),
        }

    async def _classify_one(self, ctx: dict) -> AlignmentResult | None:
        """Classify relationship between two repo instances of a concept."""
        instances = ctx["instances"]
        repos_seen = {}
        for inst in instances:
            if inst["repo"] not in repos_seen:
                repos_seen[inst["repo"]] = inst
            if len(repos_seen) >= 2:
                break
        if len(repos_seen) < 2:
            return None

        pairs = list(repos_seen.values())
        prompt = f"""Concept: {ctx["name"]}

Source A ({pairs[0]["repo"]}): Comment: {pairs[0].get("comment", "none")}, Superclass: {pairs[0].get("sup", "none")}
Source B ({pairs[1]["repo"]}): Comment: {pairs[1].get("comment", "none")}, Superclass: {pairs[1].get("sup", "none")}
Properties: {", ".join(ctx["properties"][:6]) or "none"}
Children: {", ".join(ctx["children"][:6]) or "none"}
Siblings: {", ".join(ctx["siblings"][:6]) or "none"}
Superclasses: {", ".join(ctx["superclasses"]) or "none"}

What is the relationship between concept A and concept B?"""

        try:
            raw = self.llm.generate(prompt, system=CLASSIFY_SYSTEM, max_tokens=1024)  # reasoning headroom
            line = raw.strip().split("\n")[0]
            parts = line.split("|")
            return AlignmentResult(
                class_name=ctx["name"],
                relationship=parts[0],
                confidence=float(parts[1]) if len(parts) > 1 else 0.5,
                reason=parts[2] if len(parts) > 2 else "",
                repo_a=pairs[0]["repo"],
                repo_b=pairs[1]["repo"],
            )
        except Exception as e:
            logger.warning(f"Classification failed for {ctx['name']}: {e}")
            return None

    def classify_sync(self, ctx: dict) -> AlignmentResult | None:
        """Synchronous wrapper for thread pool."""
        import asyncio

        try:
            loop = asyncio.new_event_loop()
            return loop.run_until_complete(self._classify_one(ctx))
        finally:
            loop.close()

    def run(self, config: AlignmentConfig) -> list[AlignmentResult]:
        """Run full alignment pipeline."""
        candidates = self.find_candidates(config)
        limit = config.max_pairs or len(candidates)

        # Gather context
        contexts = []
        for i, c in enumerate(candidates[:limit]):
            contexts.append(self.gather_context(c["name"]))
            if (i + 1) % 50 == 0:
                logger.info(f"Context gathered: {i + 1}/{min(limit, len(candidates))}")

        # Classify in parallel
        results = []
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            futures = {pool.submit(self.classify_sync, ctx): ctx for ctx in contexts}
            for i, f in enumerate(as_completed(futures)):
                r = f.result()
                if r:
                    results.append(r)
                if (i + 1) % 50 == 0:
                    logger.info(
                        f"Classified: {i + 1}/{len(contexts)} ({len(results)} results, {time.time() - t0:.0f}s)"
                    )

        logger.info(f"Alignment complete: {len(results)} pairs in {time.time() - t0:.0f}s")
        return results


def build_unified_ontology(
    graph_client,
    alignments: list[AlignmentResult],
    uri_prefix: str = "http://example.org/unified#",
) -> "rdflib.Graph":
    """Build a unified OWL ontology from aligned concepts.

    - Deduplicates classes by name (picks best label/comment)
    - Resolves property type conflicts by majority vote
    - Auto-declares classes referenced in domain/range
    - Adds alignment + provenance annotations
    """
    from collections import Counter

    from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef
    from rdflib.namespace import SKOS

    g = Graph()
    ns = Namespace(uri_prefix)
    g.bind("ind", ns)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)
    g.bind("skos", SKOS)

    ont = URIRef(uri_prefix.rstrip("#").rstrip("/"))
    g.add((ont, RDF.type, OWL.Ontology))

    # Declare custom annotation properties
    for ap_name in ["alignmentType", "alignmentConfidence"]:
        ap = ns[ap_name]
        g.add((ap, RDF.type, OWL.AnnotationProperty))
        g.add((ap, RDFS.label, Literal(ap_name)))

    align_map = {a.class_name: a for a in alignments}

    # Emit classes
    all_classes = graph_client.query(
        "MATCH (n:MultiClass) RETURN DISTINCT n.localName AS name, n.label AS label, "
        "n.comment AS comment, n.superclass AS sup, n.repo AS repo ORDER BY name LIMIT 15000"
    )
    by_name = defaultdict(list)
    for c in all_classes:
        by_name[c["name"]].append(c)

    emitted = set()
    for name, instances in by_name.items():
        if not name or " " in name or name in emitted:
            continue
        emitted.add(name)
        cls = ns[name]
        g.add((cls, RDF.type, OWL.Class))
        best = max(instances, key=lambda x: len(x.get("comment") or ""))
        g.add((cls, RDFS.label, Literal(best.get("label") or name)))
        if best.get("comment"):
            g.add((cls, RDFS.comment, Literal(best["comment"][:500])))
        if best.get("sup") and best["sup"].lower() not in ("none", "n/a", "na", "", "-"):
            g.add((cls, RDFS.subClassOf, ns[best["sup"]]))
        repos = sorted(set(i["repo"] for i in instances))
        g.add((cls, RDFS.isDefinedBy, Literal(", ".join(repos))))
        a = align_map.get(name)
        if a:
            g.add((cls, ns["alignmentType"], Literal(a.relationship)))
            g.add((cls, ns["alignmentConfidence"], Literal(a.confidence, datatype=XSD.decimal)))

    # Emit properties
    all_props = graph_client.query(
        "MATCH (n:MultiProp) RETURN DISTINCT n.localName AS name, n.label AS label, "
        "n.propType AS pt, n.domain AS domain, n.`range` AS rng, n.repo AS repo ORDER BY name LIMIT 15000"
    )
    prop_by_name = defaultdict(list)
    for p in all_props:
        prop_by_name[p["name"]].append(p)

    for name, instances in prop_by_name.items():
        if not name or " " in name:
            continue
        types = Counter(i["pt"] for i in instances if i.get("pt"))
        ptype = types.most_common(1)[0][0] if types else "datatype"
        prop = ns[name]
        g.add((prop, RDF.type, OWL.ObjectProperty if ptype == "object" else OWL.DatatypeProperty))
        best = max(instances, key=lambda x: len(x.get("label") or ""))
        g.add((prop, RDFS.label, Literal(best.get("label") or name)))
        domains = Counter(i["domain"] for i in instances if i.get("domain") and i["domain"].lower() not in ("none", ""))
        if domains:
            g.add((prop, RDFS.domain, ns[domains.most_common(1)[0][0]]))
        ranges = Counter(i["rng"] for i in instances if i.get("rng") and i["rng"].lower() not in ("none", ""))
        if ranges:
            rng = ranges.most_common(1)[0][0]
            xsd_map = {
                "string": XSD.string,
                "integer": XSD.integer,
                "boolean": XSD.integer,
                "dateTime": XSD.dateTime,
                "decimal": XSD.decimal,
                "float": XSD.decimal,
            }
            if ptype == "datatype" and rng in xsd_map:
                g.add((prop, RDFS.range, xsd_map[rng]))
            elif ptype == "object" and rng not in xsd_map:
                g.add((prop, RDFS.range, ns[rng]))

    # Closure: auto-declare classes referenced in domain/range but not emitted
    declared = set(g.subjects(RDF.type, OWL.Class))
    referenced: set = set()
    for _s, _p, o in g.triples((None, RDFS.domain, None)):
        if str(o).startswith(uri_prefix):
            referenced.add(o)
    for _s, _p, o in g.triples((None, RDFS.range, None)):
        if str(o).startswith(uri_prefix):
            referenced.add(o)
    for cls in referenced - declared:
        local = str(cls).rsplit("#", 1)[-1]
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal(local)))

    return g
