import { Button, Popconfirm, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import React, { useState } from "react";

import { useCredentials } from "@/app/(dashboard)/hooks/credentials/useCredentials";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { CredentialItem, credentialDeleteCall } from "../networking";
import NotificationsManager from "../molecules/notifications_manager";
import AddLoggingCredentialModal from "./AddLoggingCredentialModal";
import { LOGGING_DESTINATION_BACKENDS } from "./loggingDestinationFields";

const backendLabel = (id?: string): string =>
  LOGGING_DESTINATION_BACKENDS.find((b) => b.id === id)?.label ?? id ?? "-";

const LoggingCredentialsPanel: React.FC = () => {
  const { accessToken } = useAuthorized();
  const { data, refetch } = useCredentials();
  const [addOpen, setAddOpen] = useState(false);

  // Option A: logging destinations are credentials tagged credential_type=logging in
  // credential_info, so they live in their own surface instead of the provider registry.
  const loggingCredentials = (data?.credentials ?? []).filter(
    (c) => c.credential_info?.credential_type === "logging",
  );

  const handleDelete = async (name: string) => {
    if (!accessToken) return;
    try {
      await credentialDeleteCall(accessToken, name);
      NotificationsManager.success("Logging credential deleted");
      refetch();
    } catch (error) {
      NotificationsManager.fromBackend(error instanceof Error ? error.message : String(error));
    }
  };

  const columns: ColumnsType<CredentialItem> = [
    { title: "Name", dataIndex: "credential_name", key: "credential_name" },
    {
      title: "Destination",
      key: "destination",
      render: (_, record) => backendLabel(record.credential_info?.description),
    },
    {
      title: "Host",
      key: "host",
      render: (_, record) => record.credential_info?.host ?? "-",
    },
    {
      title: "",
      key: "actions",
      render: (_, record) => (
        <Popconfirm
          title="Delete this logging credential?"
          onConfirm={() => handleDelete(record.credential_name)}
        >
          <Button danger size="small">
            Delete
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-base font-semibold text-gray-800">Logging Credentials</div>
          <div className="text-sm text-gray-500">
            Admin-owned trace destinations. Bind one to a key or team by name from its Logging
            Settings; the inference request never carries the host or keys.
          </div>
        </div>
        <Button type="primary" onClick={() => setAddOpen(true)}>
          Add Logging Credential
        </Button>
      </div>
      <Table
        rowKey="credential_name"
        dataSource={loggingCredentials}
        columns={columns}
        pagination={false}
        locale={{ emptyText: "No logging credentials yet. Add one to bind it to a key or team." }}
      />
      {accessToken && (
        <AddLoggingCredentialModal
          accessToken={accessToken}
          open={addOpen}
          onClose={() => setAddOpen(false)}
          onCreated={() => refetch()}
        />
      )}
    </div>
  );
};

export default LoggingCredentialsPanel;
