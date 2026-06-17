export type FixExportPayload = {
  patch_path?: string;
  guide_path?: string;
  applied_to_repo?: boolean;
  apply_message?: string;
  replication_steps?: string[];
  files_changed?: string[];
  test_command?: string;
  branch_name?: string;
  commit_message?: string;
  patch_unified_diff?: string;
  test_code?: string;
};

export type WorkspaceTab = "chat" | "report" | "changes" | "input";
