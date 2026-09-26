// T-003: deterministic extraction of exactly one JSON object from possibly-noisy model
// output. This is a faithful mirror of `atlas/cognition/parse.py`; the Python test
// suite runs both implementations over the same corpus and asserts they agree.
//
// Why not a regex: real models wrap JSON in ```json fences, prepend prose, or emit
// several JSON values in a row. "Grab the first `{`" silently mis-parses the last two
// cases into a *valid-looking but wrong* object (greedy also splices an illegal one).
// This scanner therefore:
//
//   * tracks string/escape state so braces inside strings never affect depth;
//   * requires the value to be the *only* top-level value (trailing content, including
//     a second JSON value or explanatory text, is rejected);
//   * only strips a code fence when the fence wraps the WHOLE text exactly once.
//
// There is no "best guess" path: callers turn `null` into a loud error or an explicit
// "unclassified" degrade.

/** Whitespace that `String.prototype.trim` also treats as whitespace. */
const BOM = "\uFEFF";
const FENCE_LABEL = /^[A-Za-z0-9_+.-]{0,32}$/;

/** Only BOM removal + newline normalisation — never shift offsets. */
function normalizeForParse(raw) {
  let text = raw.startsWith(BOM) ? raw.slice(1) : raw;
  return text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

/** Index just past the complete JSON value starting at `start`, or -1. */
function scanValueEnd(text, start) {
  if (start >= text.length) {
    return -1;
  }
  const first = text[start];
  if (first === '"') {
    return scanStringEnd(text, start);
  }
  if (first !== "{" && first !== "[") {
    if (first === "-" || (first >= "0" && first <= "9")) {
      let i = start;
      while (i < text.length && "-+.eE0123456789".includes(text[i])) {
        i += 1;
      }
      return i > start ? i : -1;
    }
    if (text.startsWith("true", start)) return start + 4;
    if (text.startsWith("false", start)) return start + 5;
    if (text.startsWith("null", start)) return start + 4;
    return -1;
  }

  const stack = [];
  let inString = false;
  let escaped = false;
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') {
      inString = true;
      continue;
    }
    if (ch === "{" || ch === "[") {
      stack.push(ch);
      continue;
    }
    if (ch === "}" || ch === "]") {
      const open = stack.pop();
      if (open === undefined) return -1;
      if ((open === "{" && ch !== "}") || (open === "[" && ch !== "]")) return -1;
      if (stack.length === 0) return i + 1;
    }
  }
  return -1; // truncated => refuse
}

function scanStringEnd(text, start) {
  let escaped = false;
  for (let i = start + 1; i < text.length; i += 1) {
    const ch = text[i];
    if (escaped) escaped = false;
    else if (ch === "\\") escaped = true;
    else if (ch === '"') return i + 1;
  }
  return -1;
}

function parseExactlyOne(text) {
  const trimmed = text.replace(/^[\s\uFEFF]+|[\s\uFEFF]+$/g, "");
  if (trimmed === "") return null;
  const leading = text.indexOf(trimmed);
  const end = scanValueEnd(text, leading);
  if (end === -1) return null;
  if (text.slice(end).trim() !== "") return null; // trailing content => ambiguous
  let value;
  try {
    value = JSON.parse(text.slice(leading, end));
  } catch {
    return null;
  }
  // 顶层必须是对象/数组：标量不是本契约的合法形状，且与 Python 侧保持一致。
  if (value === null || typeof value !== "object") return null;
  return { value, start: leading, end };
}

/**
 * Strip a code fence only when it wraps the WHOLE text exactly once.
 *
 * Three conditions, all required: starts with ```, the info string is a plausible
 * language label, and there is exactly one closing ``` with nothing after it.
 * Anything else is malformed and is left alone, so the caller refuses it.
 */
function stripCodeFence(text) {
  const trimmed = text.replace(/^[\s\uFEFF]+|[\s\uFEFF]+$/g, "");
  if (!trimmed.startsWith("```")) return null;
  const newline = trimmed.indexOf("\n");
  if (newline === -1) return null;
  const label = trimmed.slice(3, newline).trim();
  if (!FENCE_LABEL.test(label)) return null;
  const body = trimmed.slice(newline + 1);
  const closing = body.indexOf("```");
  if (closing === -1) return null;
  const tail = body.slice(closing + 3).trim();
  if (tail !== "") return null;
  if (body.indexOf("```", closing + 3) !== -1) return null;
  const inner = body.slice(0, closing);
  if (inner.includes("```")) return null;
  return inner.trim();
}

/** Structural characters: a line containing one is NOT "pure prose" and is kept. */
const STRUCTURAL_MARKERS = ["```", "{", "}", "[", "]"];

/** Drop pure-prose wrapper lines; acceptance is still decided by `parseExactlyOne`. */
function stripSurroundingProse(text) {
  const lines = text.split("\n");
  let start = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const candidate = lines[i].trim();
    if (candidate.startsWith("{") || candidate.startsWith("[")) {
      start = i;
      break;
    }
  }
  if (start === -1) return null;
  let end = -1;
  for (let i = lines.length - 1; i >= start; i -= 1) {
    const candidate = lines[i].trim();
    if (candidate.endsWith("}") || candidate.endsWith("]")) {
      end = i;
      break;
    }
  }
  if (end === -1) return null;

  // Guard: a dropped line must contain no structural character. This is what keeps a
  // malformed fence from masquerading as "surrounding prose".
  const outside = [...lines.slice(0, start), ...lines.slice(end + 1)];
  for (const line of outside) {
    const candidate = line.trim();
    if (candidate === "") continue;
    if (STRUCTURAL_MARKERS.some((marker) => candidate.includes(marker))) {
      return null;
    }
  }

  const candidate = lines.slice(start, end + 1).join("\n").trim();
  return candidate === "" ? null : candidate;
}

/**
 * Extract exactly one JSON object/array from `raw`.
 *
 * Returns `{ value, strategy, start, end }`, or `null` when no single, complete,
 * unambiguous JSON value is present. Never returns a partial or guessed value.
 */
export function extractSingleJson(raw) {
  if (typeof raw !== "string" || raw === "") {
    return null;
  }
  const text = normalizeForParse(raw);
  const candidates = [{ text, strategy: "direct", offset: 0 }];

  const unfenced = stripCodeFence(text);
  if (unfenced !== null) {
    const at = text.indexOf(unfenced);
    candidates.push({ text: unfenced, strategy: "fence", offset: at === -1 ? 0 : at });
  }

  const prose = stripSurroundingProse(text);
  if (prose !== null) {
    const at = text.indexOf(prose);
    candidates.push({ text: prose, strategy: "prose", offset: at === -1 ? 0 : at });
  }

  for (const candidate of candidates) {
    const parsed = parseExactlyOne(candidate.text);
    if (parsed !== null) {
      return {
        value: parsed.value,
        strategy: candidate.strategy,
        start: candidate.offset + parsed.start,
        end: candidate.offset + parsed.end,
      };
    }
  }
  return null;
}
