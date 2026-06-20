import { Form, Input, Modal, Select } from "antd";
import React, { useState } from "react";

import { credentialCreateCall } from "../networking";
import NotificationsManager from "../molecules/notifications_manager";
import { LOGGING_DESTINATION_BACKENDS } from "./loggingDestinationFields";

interface AddLoggingCredentialModalProps {
  accessToken: string;
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}

const AddLoggingCredentialModal: React.FC<AddLoggingCredentialModalProps> = ({
  accessToken,
  open,
  onClose,
  onCreated,
}) => {
  const [form] = Form.useForm();
  const [backend, setBackend] = useState<string>("langfuse_otel");
  const backendDef = LOGGING_DESTINATION_BACKENDS.find((b) => b.id === backend);
  const fields = backendDef?.fields ?? [];

  const handleOk = async () => {
    let values: Record<string, string>;
    try {
      values = await form.validateFields();
    } catch {
      return; // form validation errors are shown inline
    }
    const credentialValues: Record<string, string> = {};
    fields.forEach((f) => {
      if (values[f.name]) credentialValues[f.name] = values[f.name];
    });
    const host = backendDef ? values[backendDef.hostField] : undefined;
    try {
      await credentialCreateCall(accessToken, {
        credential_name: values.credential_name,
        credential_values: credentialValues,
        // Option A: the discriminator lives in the free-form credential_info, so a
        // logging destination is filtered out of the provider registry and into the
        // logging surface without a schema migration. host is non-secret, kept here
        // so the list can show it without unmasking credential_values.
        credential_info: {
          credential_type: "logging",
          description: backend,
          ...(host ? { host } : {}),
        },
      });
      NotificationsManager.success("Logging credential created");
      form.resetFields();
      onCreated();
      onClose();
    } catch (error) {
      NotificationsManager.fromBackend(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <Modal
      title="Add Logging Credential"
      open={open}
      onCancel={onClose}
      onOk={handleOk}
      okText="Create"
      destroyOnClose
    >
      <Form form={form} layout="vertical">
        <Form.Item name="credential_name" label="Credential Name" rules={[{ required: true }]}>
          <Input placeholder="e.g. langfuse-eu" />
        </Form.Item>
        <Form.Item label="Destination Type">
          <Select
            value={backend}
            onChange={setBackend}
            options={LOGGING_DESTINATION_BACKENDS.map((b) => ({ value: b.id, label: b.label }))}
          />
        </Form.Item>
        {fields.map((f) => (
          <Form.Item
            key={f.name}
            name={f.name}
            label={f.label}
            rules={f.optional ? [] : [{ required: true }]}
          >
            {f.type === "password" ? <Input.Password /> : <Input />}
          </Form.Item>
        ))}
      </Form>
    </Modal>
  );
};

export default AddLoggingCredentialModal;
