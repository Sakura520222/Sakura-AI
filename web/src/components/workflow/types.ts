export interface Envelope<T> {
  success: boolean;
  message: string;
  data: T;
}
export interface PageData<T> {
  items: T[];
  total: number;
  page: number;
  total_pages: number;
  per_page: number;
}

export interface Review {
  id: number;
  pr_id: number;
  repo_name: string | null;
  repo_owner: string | null;
  author: string | null;
  title: string | null;
  status: string | null;
  decision: string | null;
  overall_score: number | null;
  strategy: string | null;
  created_at: string | null;
  completed_at: string | null;
  branch?: string | null;
  file_count?: number | null;
  line_count?: number | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  review_summary?: string | null;
  decision_reason?: string | null;
  error_message?: string | null;
  comments?: ReviewComment[];
}

export interface ReviewComment {
  id: number;
  file_path: string | null;
  line_number: number | null;
  comment_type: string | null;
  severity: string | null;
  content: string;
  created_at: string | null;
}

export interface Issue {
  id: number;
  issue_number: number;
  repo_name: string | null;
  repo_owner: string | null;
  author: string | null;
  title: string | null;
  category: string | null;
  priority: string | null;
  status: string | null;
  summary: string | null;
  feasibility: string | null;
  suggested_title: string | null;
  suggested_labels: Array<{ name: string; confidence?: number }>;
  suggested_assignees: Array<{ username: string; reason?: string }>;
  suggested_milestone: string | null;
  related_prs: Array<{
    number?: number;
    pr_number?: number;
    title?: string;
    reason?: string;
    url?: string;
  }>;
  duplicate_of: number | null;
  analysis_detail: string | null;
  error_message: string | null;
  comment_url: string | null;
  created_at: string | null;
  completed_at: string | null;
  issue_state?: string | null;
  issue_relations?: {
    status?: string;
    primary?: IssueRelation | null;
    related?: IssueRelation[];
  } | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
}

export interface IssueRelation {
  number: number;
  relation: string;
  confidence: number;
  reason: string;
  similarities?: string[];
  differences?: string[];
  evidence?: Array<{ current_quote: string; candidate_quote: string }>;
}
