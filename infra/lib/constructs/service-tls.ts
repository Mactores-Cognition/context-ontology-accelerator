// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as acmpca from "aws-cdk-lib/aws-acmpca";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

import { SERVICE_CERT_PRIVATE_CA, type ServiceTlsSettings } from "../context";

export interface ServiceTlsProps {
  /** VPC the private hosted zone is attached to. */
  readonly vpc: ec2.IVpc;
  /** Resolved settings (`resolveServiceTls`). */
  readonly settings: ServiceTlsSettings;
  /**
   * SSM parameter for the CA root certificate (private CA only); callers add
   * it to their trust store.
   */
  readonly caCertParameterName?: string;
}

/**
 * Names and a certificate for COA's own services behind the internal ALB
 * (`service_discovery=alb`): a Route 53 private hosted zone, and a certificate
 * for `*.<zone>` from AWS Private CA (default) or a public ACM certificate.
 *
 * The private CA is a general-purpose root (ACM cannot issue from a
 * short-lived-mode CA). CloudFormation deletes the activation first on stack
 * delete, which disables the CA so the CA itself can then be deleted.
 */
export class ServiceTls extends Construct {
  /** Private hosted zone the service records live in. */
  public readonly zone: route53.IPrivateHostedZone;
  /** Certificate for `*.<zone>`, attached to the ALB's HTTPS listeners. */
  public readonly certificate: acm.ICertificate;
  /** Zone name, e.g. `coa-dev.internal`. */
  public readonly domainName: string;

  constructor(scope: Construct, id: string, props: ServiceTlsProps) {
    super(scope, id);
    const { settings } = props;
    this.domainName = settings.domainName;

    this.zone = new route53.PrivateHostedZone(this, "Zone", {
      zoneName: settings.domainName,
      vpc: props.vpc,
    });

    if (settings.mode === SERVICE_CERT_PRIVATE_CA) {
      this.certificate = this.privateCaCertificate(props);
    } else {
      const publicZone = route53.HostedZone.fromHostedZoneAttributes(
        this,
        "PublicZone",
        { hostedZoneId: settings.publicZoneId!, zoneName: settings.domainName },
      );
      this.certificate = new acm.Certificate(this, "Certificate", {
        domainName: `*.${settings.domainName}`,
        validation: acm.CertificateValidation.fromDns(publicZone),
      });
    }
  }

  private privateCaCertificate(props: ServiceTlsProps): acm.ICertificate {
    const partition = cdk.Stack.of(this).partition;
    const ca = new acmpca.CfnCertificateAuthority(this, "RootCa", {
      type: "ROOT",
      usageMode: "GENERAL_PURPOSE",
      keyAlgorithm: "RSA_2048",
      signingAlgorithm: "SHA256WITHRSA",
      subject: { commonName: `${props.settings.domainName} root CA` },
    });
    const caCert = new acmpca.CfnCertificate(this, "RootCaCertificate", {
      certificateAuthorityArn: ca.attrArn,
      certificateSigningRequest: ca.attrCertificateSigningRequest,
      signingAlgorithm: "SHA256WITHRSA",
      templateArn: `arn:${partition}:acm-pca:::template/RootCACertificate/V1`,
      validity: { type: "YEARS", value: 10 },
    });
    const activation = new acmpca.CfnCertificateAuthorityActivation(
      this,
      "RootCaActivation",
      {
        certificateAuthorityArn: ca.attrArn,
        certificate: caCert.attrCertificate,
        status: "ACTIVE",
      },
    );
    // ACM issues and renews the certificate on the CA's behalf.
    const permission = new acmpca.CfnPermission(this, "AcmPermission", {
      certificateAuthorityArn: ca.attrArn,
      principal: "acm.amazonaws.com",
      actions: ["IssueCertificate", "GetCertificate", "ListPermissions"],
    });

    const certificate = new acm.PrivateCertificate(this, "Certificate", {
      domainName: `*.${props.settings.domainName}`,
      certificateAuthority:
        acmpca.CertificateAuthority.fromCertificateAuthorityArn(
          this,
          "RootCaRef",
          ca.attrArn,
        ),
    });
    certificate.node.addDependency(activation, permission);

    if (props.caCertParameterName) {
      new ssm.StringParameter(this, "CaCertParameter", {
        parameterName: props.caCertParameterName,
        stringValue: caCert.attrCertificate,
        description:
          "Root certificate of the private CA for COA's internal services (PEM)",
      });
    }
    return certificate;
  }
}
