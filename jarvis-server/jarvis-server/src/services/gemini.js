const { GoogleGenerativeAI } = require("@google/generative-ai");

let genAI = null;
let model = null;

function initGemini() {
  if (!process.env.GEMINI_API_KEY) {
    throw new Error(
      "GEMINI_API_KEY is not set. Add it to .env to enable the Groq fallback. " +
      "Get a free key at https://aistudio.google.com/app/apikey"
    );
  }
  genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
  model = genAI.getGenerativeModel({
    model: process.env.GEMINI_MODEL || "gemini-2.0-flash",
  });
  console.log("[Gemini] Fallback initialized.");
}

/**
 * Query Gemini 2.0 Flash as a drop-in fallback for Groq.
 *
 * @param {string} systemPrompt   - The full JARVIS system prompt (with memory/time injected)
 * @param {Array}  history        - conversationHistory array from groq.js
 * @param {string} userMessage    - The current (possibly web-enriched) user message
 * @returns {Promise<string>}     - JARVIS response text
 */
async function queryGemini(systemPrompt, history, userMessage) {
  if (!model) initGemini();

  // Convert Groq-style history [{role:"user"|"assistant", content}]
  // to Gemini-style [{role:"user"|"model", parts:[{text}]}]
  const geminiHistory = history.map(msg => ({
    role: msg.role === "assistant" ? "model" : "user",
    parts: [{ text: msg.content }],
  }));

  const chat = model.startChat({
    systemInstruction: systemPrompt,
    history: geminiHistory,
    generationConfig: {
      maxOutputTokens: 300,
      temperature: 0.8,
    },
  });

  const result   = await chat.sendMessage(userMessage);
  const response = result.response.text();
  return response;
}

module.exports = { queryGemini };