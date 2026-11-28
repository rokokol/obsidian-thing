import { createServer } from "node:http";
import { expect, it } from "vitest";
import { McpClient } from "./client";
import { transport } from "./transport";

it("does not follow redirects or send a bearer token to the redirect target", async () => {
  let targetCalls = 0;
  const server = createServer((request, response) => {
    request.resume();
    if (request.url === "/mcp") {
      response.writeHead(307, { Location: "/target" }).end();
    } else {
      targetCalls++;
      response.writeHead(200).end("{}");
    }
  });
  try {
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject).listen(0, "127.0.0.1", resolve);
    });
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("Expected a TCP port");
    const client = new McpClient(`http://127.0.0.1:${address.port}/mcp`, "sample-token", transport);
    await expect(client.tools()).rejects.toThrow("Redirects are disabled");
    expect(targetCalls).toBe(0);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
