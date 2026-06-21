import { Form, Input, Modal, Select } from "antd";
import React, { useMemo, useState } from "react";

import { CredentialAccess } from "../Settings/LoggingAndAlerts/LoggingCallbacks/types";
import NotificationsManager from "../molecules/notifications_manager";
import AccessControlFields from "./AccessControlFields";
import { createLoggingCredential, LOGGING_BACKEND_IDS } from "./loggingCredentialApi";
import { LOGGING_DESTINATION_BACKENDS } from "./loggingDestinationFields";

interface AvailableCallback {
  ui_callback_name: string;
}

interface UnifiedAddLoggingModalProps {
  accessToken: string;
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
  availableCallbacks: Record<string, AvailableCallback>;
  onSelectConfigCallback: (id: string) => void;
}

// One "+ Add" for every logging target. OTEL trace destinations are created inline as
// admin-owned credentials (with access); non-OTEL config callbacks hand off to the
// existing global-callback flow, so the two are not split into separate buttons.
const UnifiedAddLoggingModal: React.FC<UnifiedAddLoggingModalProps> = ({
  accessToken,
  open,
  onClose,
  onCreated,
  availableCallbacks,
  onSelectConfigCallback,
}) => {
  const [form] = Form.useForm();
  const [selected, setSelected] = useState<string>("langfuse_otel");
  const [access, setAccess] = useState<CredentialAccess>({});

  const isDestination = LOGGING_BACKEND_IDS.has(selected);
  const backendDef = LOGGING_DESTINATION_BACKENDS.find((b) => b.id === selected);
  const fields = backendDef?.fields ?? [];

  const options = useMemo(
    () => [
      {
        label: "Trace destinations (OTEL)",
        options: LOGGING_DESTINATION_BACKENDS.map((b) => ({ value: b.id, label: b.label })),
      },
      {
        label: "Other callbacks",
        options: Object.entries(availableCallbacks)
          .filter(([id]) => !LOGGING_BACKEND_IDS.has(id))
          .map(([id, cb]) => ({ value: id, label: cb.ui_callback_name || id })),
      },
    ],
    [availableCallbacks],
  );

  const reset = () => {
    form.resetFields();
    setAccess({});
  };

  const handleOk = async () => {
    if (!isDestination) {
      onSelectConfigCallback(selected);
      reset();
      onClose();
      return;
    }
    let values: Record<string, string>;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const credentialValues = Object.fromEntries(
      fields.filter((f) => values[f.name]).map((f) => [f.name, values[f.name]]),
    );
    const host = backendDef ? values[backendDef.hostField] : undefined;
    try {
      await createLoggingCredential(accessToken, {
        credentialName: values.credential_name,
        backend: selected,
        values: credentialValues,
        host,
        access: access.global || access.teams?.length || access.orgs?.length ? access : undefined,
      });
      NotificationsManager.success("Logging destination created");
      reset();
      onCreated();
      onClose();
    } catch (error) {
      NotificationsManager.fromBackend(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <Modal
      title="Add Logging Destination"
      open={open}
      onCancel={() => {
        reset();
        onClose();
      }}
      onOk={handleOk}
      okText={isDestination ? "Create" : "Continue"}
      destroyOnClose
    >
      <Form form={form} layout="vertical">
        <Form.Item label="Type">
          <Select value={selected} onChange={setSelected} options={options} optionFilterProp="label" showSearch />
        </Form.Item>
        {isDestination ? (
          <>
            <Form.Item name="credential_name" label="Name" rules={[{ required: true }]}>
              <Input placeholder="e.g. langfuse-eu" />
            </Form.Item>
            {fields.map((f) => (
              <Form.Item key={f.name} name={f.name} label={f.label} rules={f.optional ? [] : [{ required: true }]}>
                {f.type === "password" ? <Input.Password /> : <Input />}
              </Form.Item>
            ))}
            <AccessControlFields value={access} onChange={setAccess} />
          </>
        ) : (
          <div className="text-sm text-gray-500">Continue to configure this callback.</div>
        )}
      </Form>
    </Modal>
  );
};

export default UnifiedAddLoggingModal;
