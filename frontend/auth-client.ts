/** Same-origin JWT Bearer client. Tokens live only in this instance's memory. */
export interface Principal {
  user_id: string;
  tenant_id: string;
  username: string;
  permissions: string[];
}

export interface ManagedUser extends Principal {
  active: boolean;
  created_at: string;
}

export interface LoginInput {
  tenant_id: string;
  username: string;
  password: string;
}

export interface RegisterInput {
  username: string;
  password: string;
  permissions?: string[];
}

export interface UserUpdate {
  active?: boolean;
  permissions?: string[];
}

interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string };
  request_id?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

export class AuthClient {
  private accessToken: string | null = null;
  private expiresAt = 0;
  private readonly apiBase: string;
  private readonly onUnauthorized: (() => void) | undefined;

  constructor(
    apiBase = "/api/v1",
    onUnauthorized?: () => void,
  ) {
    if (!apiBase.startsWith("/") || apiBase.startsWith("//") || apiBase.includes("?")) {
      throw new Error("apiBase must be a same-origin absolute path");
    }
    this.apiBase = apiBase.replace(/\/$/, "");
    this.onUnauthorized = onUnauthorized;
  }

  /** A UI hint only; the server checks sessions and permissions on every request. */
  get hasSession(): boolean {
    return this.accessToken !== null && Date.now() < this.expiresAt;
  }

  async login(input: LoginInput): Promise<Principal> {
    const token = await this.json<TokenResponse>(
      "/identity/login",
      { method: "POST", body: JSON.stringify(input) },
      false,
    );
    if (
      token.token_type !== "bearer" ||
      typeof token.access_token !== "string" ||
      !token.access_token ||
      !Number.isFinite(token.expires_in) ||
      token.expires_in <= 0
    ) {
      throw new Error("Invalid token response");
    }
    this.accessToken = token.access_token;
    this.expiresAt = Date.now() + token.expires_in * 1000;
    return this.me();
  }

  me(): Promise<Principal> {
    return this.json<Principal>("/identity/me");
  }

  register(input: RegisterInput): Promise<ManagedUser> {
    return this.json<ManagedUser>("/identity/register", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  listUsers(limit = 100): Promise<ManagedUser[]> {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      throw new Error("limit must be an integer between 1 and 100");
    }
    return this.json<ManagedUser[]>(`/identity/users?limit=${limit}`);
  }

  updateUser(userId: string, update: UserUpdate): Promise<ManagedUser> {
    return this.json<ManagedUser>(`/identity/users/${encodeURIComponent(userId)}`, {
      method: "PATCH",
      body: JSON.stringify(update),
    });
  }

  async logout(): Promise<void> {
    const token = this.accessToken;
    try {
      if (token !== null) {
        await this.authorizedFetch("/identity/logout", { method: "POST" });
      }
    } finally {
      // A failed network request clears this client; the server session may still
      // exist until its expiry. A concurrent new login must keep its own token.
      if (this.accessToken === token) this.clearSession();
    }
  }

  /** For same-origin application APIs, including streaming export responses. */
  authorizedFetch(path: string, init: RequestInit = {}): Promise<Response> {
    return this.send(path, init, true);
  }

  private clearSession(): void {
    this.accessToken = null;
    this.expiresAt = 0;
  }

  private async json<T>(
    path: string,
    init: RequestInit = {},
    authenticated = true,
  ): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body !== undefined) headers.set("Content-Type", "application/json");
    const response = await this.send(path, { ...init, headers }, authenticated);
    return (await response.json()) as T;
  }

  private async send(
    path: string,
    init: RequestInit,
    authenticated: boolean,
  ): Promise<Response> {
    if (!path.startsWith("/") || path.startsWith("//")) {
      throw new Error("API path must be relative to apiBase");
    }
    const url = new URL(`${this.apiBase}${path}`, globalThis.location.origin);
    if (url.origin !== globalThis.location.origin) {
      throw new Error("Cross-origin API requests are not allowed");
    }
    const token = authenticated ? this.accessToken : null;
    if (authenticated && token === null) {
      throw new ApiError(401, "not_authenticated", "Login required", null);
    }
    const headers = new Headers(init.headers);
    if (token !== null) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(url, {
      ...init,
      headers,
      credentials: "omit",
      redirect: "error",
    });
    if (response.ok) return response;
    if (response.status === 401 && token !== null && this.accessToken === token) {
      this.clearSession();
      this.onUnauthorized?.();
    }
    let body: ErrorEnvelope = {};
    try {
      body = (await response.json()) as ErrorEnvelope;
    } catch {
      // Intermediaries can return non-JSON errors.
    }
    throw new ApiError(
      response.status,
      body.error?.code ?? "http_error",
      body.error?.message ?? `HTTP ${response.status}`,
      body.request_id ?? response.headers.get("X-Request-ID"),
    );
  }
}
