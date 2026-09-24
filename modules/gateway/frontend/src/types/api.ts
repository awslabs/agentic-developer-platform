// Generic API response wrappers

export interface ApiError {
  error: string;
  message: string;
  details?: Record<string, unknown>;
  /**
   * HTTP status of the failed response, attached by `ApiClient.handleResponse`.
   *
   * Optional because the thrown value is the server's parsed error body, which
   * does not itself carry the status. Callers need it to tell "you are not
   * permitted" from "this is not deployed here" from "retry shortly" — three
   * outcomes that can share an identical body.
   */
  status?: number;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
}

export interface ApiResponse<T> {
  data?: T;
  error?: ApiError;
}

// Request options
export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  headers?: Record<string, string>;
  body?: unknown;
  signal?: AbortSignal;
}

// Pagination params
export interface PaginationParams {
  page?: number;
  pageSize?: number;
}

// Sort params
export interface SortParams {
  sortBy?: string;
  sortOrder?: 'asc' | 'desc';
}

// Common filter params
export interface DateRangeFilter {
  startTime?: string;
  endTime?: string;
}
