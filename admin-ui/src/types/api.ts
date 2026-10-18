export interface ApiResponse<T> {
  data: T;
  meta: {
    requestId: string;
    timestamp: string;
  };
  pagination?: {
    cursor?: string;
    hasMore: boolean;
    total?: number;
  };
}
