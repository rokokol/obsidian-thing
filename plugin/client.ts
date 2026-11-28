export const DEFAULT_ENDPOINT = "http://127.0.0.1:8765/vaults/notes/mcp";

export type Transport = (url: string, headers: Record<string, string>, body: string) =>
  Promise<{ status: number; text: string }>;

type Envelope = { jsonrpc?: unknown; id?: unknown; result?: unknown; error?: unknown };

export class McpClient {
  private readonly url: string;
  private id = 0;

  constructor(endpoint: string, private readonly token: string, private readonly request: Transport) {
    let url: URL;
    try {
      url = new URL(endpoint.trim());
    } catch {
      throw new Error("Enter a valid MCP endpoint URL");
    }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new Error("Use an HTTP or HTTPS endpoint without credentials, a query or a fragment");
    }
    if (!/^[a-zA-Z0-9._~+/=-]+$/.test(token)) throw new Error("Select a valid vault token");
    this.url = url.href;
  }

  private async rpc<T>(method: string, params: Record<string, unknown>): Promise<T> {
    const id = ++this.id;
    let response: Awaited<ReturnType<Transport>>;
    try {
      response = await this.request(this.url, {
        Authorization: `Bearer ${this.token}`,
        Accept: "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-11-25",
        "X-Obsidian-Thing-Client": "plugin",
      }, JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    } catch {
      throw new Error("Cannot reach the MCP endpoint or read its response");
    }
    if (response.status === 401 || response.status === 403) throw new Error("Authorization failed. Check the vault token");
    if (response.status >= 300 && response.status < 400) throw new Error("Use the final endpoint URL. Redirects are disabled");
    if (response.status !== 200) throw new Error(`MCP endpoint returned HTTP ${response.status}`);
    let envelope: Envelope;
    try {
      envelope = JSON.parse(response.text);
    } catch {
      throw new Error("The endpoint did not return MCP JSON");
    }
    if (envelope?.jsonrpc !== "2.0" || envelope.id !== id || envelope.error || envelope.result == null) {
      throw new Error("The endpoint did not return a valid MCP result");
    }
    return envelope.result as T;
  }

  async tools(): Promise<Set<string>> {
    const result = await this.rpc<{ tools?: { name: string }[] }>("tools/list", {});
    if (!Array.isArray(result.tools)) throw new Error("The endpoint did not return its tool list");
    return new Set(result.tools.map(({ name }) => name));
  }

  async call<T>(name: string, args: Record<string, unknown> = {}): Promise<T> {
    const result = await this.rpc<{
      structuredContent?: T;
      isError?: boolean;
      content?: { type: string; text?: string }[];
    }>("tools/call", { name, arguments: args });
    if (result.isError) {
      const detail = result.content?.find(part => part.type === "text")?.text;
      throw new Error(detail?.replaceAll(this.token, "[redacted]").slice(0, 500) || "The MCP tool refused the request");
    }
    if (result.structuredContent === undefined) throw new Error("The tool did not return structured content");
    return result.structuredContent;
  }
}
