// 公式 OpenAI Node.js SDK から使う例。
//   npm install openai
//   node examples/node_openai.mjs
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: process.env.OPENAI_BASE_URL ?? "http://localhost:8765/v1",
  apiKey: process.env.OPENAI_API_KEY ?? "dummy",
});
const model = process.env.MODEL ?? "default";

const res = await client.chat.completions.create({
  model,
  messages: [{ role: "user", content: "TypeScriptの型ガードを一文で説明して" }],
});
console.log("chat:", res.choices[0].message.content);

const stream = await client.chat.completions.create({
  model,
  stream: true,
  messages: [{ role: "user", content: "1から5まで数えて" }],
});
process.stdout.write("stream: ");
for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
console.log();

const r = await client.responses.create({ model, input: "HTTPのステータスコード418とは？" });
console.log("responses:", r.output_text);
