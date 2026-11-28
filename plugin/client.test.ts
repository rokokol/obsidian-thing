import { describe, expect, it, vi } from "vitest";
import { DEFAULT_ENDPOINT, McpClient } from "./client";

describe("MCP client", () => {
  it("discovers the server tools with the plugin header", async () => {
    const request = vi.fn(async (_url: string, _headers: Record<string, string>, body: string) => {
      const { id, method } = JSON.parse(body);
      expect(method).toBe("tools/list");
      return { status: 200, text: JSON.stringify({ jsonrpc: "2.0", id, result: {
        tools: [{ name: "obsidian.query" }],
      } }) };
    });
    expect(await new McpClient(DEFAULT_ENDPOINT, "sample-token", request).tools()).toEqual(new Set(["obsidian.query"]));
    expect(request.mock.calls[0][1]).toMatchObject({
      Authorization: "Bearer sample-token",
      "X-Obsidian-Thing-Client": "plugin",
    });
  });

  it("returns structured results and reports tool failures", async () => {
    const request = vi.fn(async (_url: string, _headers: Record<string, string>, body: string) => {
      const { id, params } = JSON.parse(body);
      const result = params.arguments.text ? { structuredContent: { matches: [] } }
        : { isError: true, content: [{ text: "No source", type: "text" }] };
      return { status: 200, text: JSON.stringify({ jsonrpc: "2.0", id, result }) };
    });
    const client = new McpClient(DEFAULT_ENDPOINT, "sample-token", request);
    expect(await client.call("obsidian.query", { text: "notes" })).toEqual({ matches: [] });
    await expect(client.call("obsidian.related")).rejects.toThrow("No source");
  });

  it("rejects credentials in URLs and does not expose HTTP response text on auth errors", async () => {
    const request = vi.fn(async () => ({ status: 401, text: "private response" }));
    expect(() => new McpClient("http://name:secret@example.com/mcp", "sample-token", request)).toThrow();
    await expect(new McpClient(DEFAULT_ENDPOINT, "sample-token", request).tools()).rejects.toThrow("Authorization");
  });
});
