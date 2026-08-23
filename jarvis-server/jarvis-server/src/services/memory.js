const fs   = require("fs");
const path = require("path");
const Groq = require("groq-sdk");

const MEMORY_FILE = path.join(__dirname, "../../memory.json");

// ── Helpers ───────────────────────────────────────────────────────────────────

function loadMemory() {
  try {
    if (fs.existsSync(MEMORY_FILE)) {
      return JSON.parse(fs.readFileSync(MEMORY_FILE, "utf8"));
    }
  } catch (e) {
    console.error("[Memory] Failed to load memory file:", e.message);
  }
  return { facts: [], updatedAt: null };
}

function saveMemory(memory) {
  try {
    memory.updatedAt = new Date().toISOString();
    fs.writeFileSync(MEMORY_FILE, JSON.stringify(memory, null, 2), "utf8");
  } catch (e) {
    console.error("[Memory] Failed to save memory file:", e.message);
  }
}

/**
 * Format the saved facts into a compact string to inject into the system prompt.
 * Costs ~30-60 tokens regardless of how many facts are stored.
 */
function formatMemoryForPrompt() {
  const memory = loadMemory();
  if (!memory.facts || memory.facts.length === 0) return null;

  const lines = memory.facts.map(f => `- ${f.key}: ${f.value}`).join("\n");
  return `Things you already know about the user (recalled from past sessions):\n${lines}`;
}

// ── Memory extraction (silent AI call) ───────────────────────────────────────

const EXTRACTION_PROMPT = `You are a memory extraction system for an AI assistant called JARVIS.
Your job is to read a single conversation exchange and decide if it contains anything worth remembering long-term.

Things worth remembering:
- The user's name or what they like to be called
- The user's job, role, location, or life context
- Explicit preferences ("I prefer...", "I like...", "I hate...", "always/never do X")
- Important personal facts (family, health, major projects, goals)
- Technical context (what tools/languages they use, their setup)

Things NOT worth remembering:
- General knowledge questions ("what is X", "how does Y work")
- Casual one-off exchanges or small talk
- Anything that is temporary or time-specific
- Anything already known (check existing facts before adding duplicates)

Respond with ONLY valid JSON — no explanation, no markdown, no backticks.

If something is worth remembering, respond with:
{"remember": true, "key": "short label", "value": "concise fact to store"}

If nothing is worth remembering, respond with:
{"remember": false}`;

async function extractAndSaveMemory(client, model, userMessage, assistantReply) {
  try {
    const existing = loadMemory();
    const existingFacts = existing.facts.length > 0
      ? `Existing known facts:\n${existing.facts.map(f => `- ${f.key}: ${f.value}`).join("\n")}`
      : "No existing facts yet.";

    const exchangeSummary = `User said: "${userMessage}"\nJARVIS replied: "${assistantReply}"`;

    const result = await client.chat.completions.create({
      model,
      messages: [
        { role: "system", content: EXTRACTION_PROMPT },
        { role: "user",   content: `${existingFacts}\n\nNew exchange:\n${exchangeSummary}` },
      ],
      max_tokens: 80,
      temperature: 0.1, // deterministic — this is a classification task
    });

    const raw = result.choices[0].message.content.trim();
    const parsed = JSON.parse(raw);

    if (parsed.remember === true && parsed.key && parsed.value) {
      const memory = loadMemory();

      // Update if key already exists, otherwise append
      const existingIdx = memory.facts.findIndex(
        f => f.key.toLowerCase() === parsed.key.toLowerCase()
      );
      if (existingIdx >= 0) {
        memory.facts[existingIdx].value = parsed.value;
        console.log(`[Memory] Updated: "${parsed.key}" → "${parsed.value}"`);
      } else {
        memory.facts.push({ key: parsed.key, value: parsed.value });
        console.log(`[Memory] Saved: "${parsed.key}" → "${parsed.value}"`);
      }

      saveMemory(memory);
    } else {
      console.log("[Memory] Nothing to remember from this exchange.");
    }
  } catch (e) {
    // Silent fail — memory extraction is non-critical
    console.error("[Memory] Extraction failed:", e.message);
  }
}

module.exports = { formatMemoryForPrompt, extractAndSaveMemory, loadMemory, saveMemory };
