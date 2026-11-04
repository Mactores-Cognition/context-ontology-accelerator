// "Custom actions" menu on the source page: add-on actions that are not part of
// standard COA, kept together so they are easy to tell apart (and to remove).
// To add an action: one item below plus the dialog it opens.

import React, { useState } from "react";
import ButtonDropdown, {
  type ButtonDropdownProps,
} from "@cloudscape-design/components/button-dropdown";
import {
  KeysImportDialog,
  type KeysImportActionProps,
} from "@components/KeysImport/KeysImportButton";

/** Short on purpose: shown as a tooltip on the greyed-out item. */
export const KEYS_IMPORT_DISABLED_REASON =
  "Available once tables are in review or approved";

const IMPORT_KEYS = "import-keys";

export interface CustomActionsMenuProps {
  namespaceId: string;
  sourceId: string;
  /** "Import keys (CSV)" (database sources). */
  keysImport: KeysImportActionProps;
}

/**
 * "Custom actions" dropdown. Uses no API hooks itself: an action's dialog (and
 * every API call it makes) exists only while it is open.
 */
export function CustomActionsMenu({
  namespaceId,
  sourceId,
  keysImport,
}: CustomActionsMenuProps) {
  const [open, setOpen] = useState<string | null>(null);
  const { disabled, ...keysImportDialogProps } = keysImport;

  const items: ButtonDropdownProps.Items = [
    {
      id: IMPORT_KEYS,
      text: "Import keys (CSV)",
      disabled: !!disabled,
      disabledReason: disabled ? KEYS_IMPORT_DISABLED_REASON : undefined,
    },
  ];

  return (
    <>
      <ButtonDropdown
        items={items}
        onItemClick={({ detail }) => setOpen(detail.id)}
      >
        Custom actions
      </ButtonDropdown>
      {open === IMPORT_KEYS && (
        <KeysImportDialog
          namespaceId={namespaceId}
          sourceId={sourceId}
          {...keysImportDialogProps}
          onClose={() => setOpen(null)}
        />
      )}
    </>
  );
}
