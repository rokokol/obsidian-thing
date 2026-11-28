import * as http from "node:http";
import * as https from "node:https";
import type { Transport } from "./client";

export const transport: Transport = (url, headers, body) => new Promise((resolve, reject) => {
  const { request } = url.startsWith("https:") ? https : http;
  const req = request(url, {
    method: "POST",
    headers: { ...headers, "Content-Length": Buffer.byteLength(body) },
    signal: AbortSignal.timeout(60_000),
  }, (response) => {
    const status = response.statusCode ?? 0;
    if (status !== 200) {
      response.destroy();
      resolve({ status, text: "" });
      return;
    }
    const chunks: Buffer[] = [];
    let bytes = 0;
    response.on("data", (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > 8 * 1024 * 1024) {
        response.destroy(new Error("Response exceeds the size limit"));
        return;
      }
      chunks.push(chunk);
    });
    response.on("error", reject);
    response.on("end", () => resolve({ status, text: Buffer.concat(chunks).toString("utf8") }));
  });
  req.on("error", reject);
  req.end(body);
});
