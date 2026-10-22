// 移植自 pyJianYingDraft（Apache-2.0）与 JianYingAPI 的草稿写出约定。
// 剪映草稿是 Python json.dumps 写的：浮点数一律带小数点（1.0）、整数不带。
// JSON.stringify 会把 1.0 写成 1，所以这里单独实现一份与 Python 输出逐字节一致的序列化：
// 需要按浮点写出的整数值用 PyFloat 包起来，其余非整数的 number 也按浮点写。

export class PyFloat {
  constructor(value) {
    this.value = Number(value);
  }

  valueOf() {
    return this.value;
  }

  toJSON() {
    return this.value;
  }
}

/** Python 侧是 float 的值：整数值也要写成 1.0。 */
export function pyFloat(value) {
  return value instanceof PyFloat ? value : new PyFloat(value);
}

/** 同 Python float.__repr__（最短往返位数；指数在 decpt<=-4 或 >16 时启用）。 */
export function floatRepr(value) {
  if (Number.isNaN(value)) return "NaN";
  if (value === Infinity) return "Infinity";
  if (value === -Infinity) return "-Infinity";
  if (value === 0) return Object.is(value, -0) ? "-0.0" : "0.0";
  const sign = value < 0 ? "-" : "";
  const [mantissa, exponent] = Math.abs(value).toExponential().split("e");
  const digits = mantissa.replace(".", "");
  const decpt = Number(exponent) + 1;
  if (decpt <= -4 || decpt > 16) {
    const power = decpt - 1;
    const body = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    return `${sign}${body}e${power < 0 ? "-" : "+"}${String(Math.abs(power)).padStart(2, "0")}`;
  }
  if (decpt <= 0) return `${sign}0.${"0".repeat(-decpt)}${digits}`;
  if (decpt >= digits.length) return `${sign}${digits}${"0".repeat(decpt - digits.length)}.0`;
  return `${sign}${digits.slice(0, decpt)}.${digits.slice(decpt)}`;
}

function intText(value) {
  return Math.abs(value) < 1e21 ? String(value) : BigInt(value).toString();
}

/**
 * 同 Python json.dumps(value, ensure_ascii=False, indent=..., sort_keys=..., allow_nan=...)。
 * indent 为 null 时分隔符是 ", " / ": "；有 indent 时是 "," / ": "（Python 的默认规则）。
 */
export function dumps(value, { indent = null, sortKeys = false, separators = null, allowNaN = true } = {}) {
  const [itemSeparator, keySeparator] = separators ?? (indent === null ? [", ", ": "] : [",", ": "]);
  const unit = indent === null ? null : " ".repeat(indent);

  const number = (raw) => {
    if (!Number.isFinite(raw) && !allowNaN) {
      throw new RangeError(`Out of range float values are not JSON compliant: ${raw}`);
    }
    return floatRepr(raw);
  };

  const encode = (item, level) => {
    if (item === null || item === undefined) return "null";
    if (item === true) return "true";
    if (item === false) return "false";
    if (item instanceof PyFloat) return number(item.value);
    if (typeof item === "number") {
      return Number.isInteger(item) ? intText(item) : number(item);
    }
    if (typeof item === "string") return JSON.stringify(item);
    const open = unit === null ? "" : `\n${unit.repeat(level + 1)}`;
    const close = unit === null ? "" : `\n${unit.repeat(level)}`;
    const between = unit === null ? itemSeparator : `${itemSeparator}\n${unit.repeat(level + 1)}`;
    if (Array.isArray(item)) {
      if (item.length === 0) return "[]";
      return `[${open}${item.map((entry) => encode(entry, level + 1)).join(between)}${close}]`;
    }
    if (typeof item === "object") {
      // 与 JSON.stringify 一样跳过值为 undefined 的键（JS 对象里的「没有这个字段」）
      const keys = Object.keys(item).filter((key) => item[key] !== undefined);
      if (sortKeys) keys.sort();
      if (keys.length === 0) return "{}";
      const parts = keys.map((key) => `${JSON.stringify(key)}${keySeparator}${encode(item[key], level + 1)}`);
      return `{${open}${parts.join(between)}${close}}`;
    }
    throw new TypeError(`Object of type ${typeof item} is not JSON serializable`);
  };

  return encode(value, 0);
}

/**
 * 解析 JSON，并把「写成小数形式的整数值」（如 30.0、1e+16）包成 PyFloat，
 * 这样读进来再用 dumps 写回时与 Python 的 json.loads → json.dumps 一致。
 */
export function loads(text) {
  let index = 0;
  const fail = (message) => {
    throw new SyntaxError(`${message} at position ${index}`);
  };
  const space = () => {
    while (index < text.length && " \t\n\r".includes(text[index])) index += 1;
  };
  const literal = (word, result) => {
    if (text.startsWith(word, index)) {
      index += word.length;
      return result;
    }
    return fail("Unexpected token");
  };
  const parseValue = () => {
    space();
    const char = text[index];
    if (char === "{") {
      index += 1;
      const result = {};
      space();
      if (text[index] === "}") {
        index += 1;
        return result;
      }
      for (;;) {
        space();
        if (text[index] !== "\"") fail("Expected property name");
        const key = parseString();
        space();
        if (text[index] !== ":") fail("Expected ':'");
        index += 1;
        result[key] = parseValue();
        space();
        if (text[index] === ",") {
          index += 1;
          continue;
        }
        if (text[index] === "}") {
          index += 1;
          return result;
        }
        fail("Expected ',' or '}'");
      }
    }
    if (char === "[") {
      index += 1;
      const result = [];
      space();
      if (text[index] === "]") {
        index += 1;
        return result;
      }
      for (;;) {
        result.push(parseValue());
        space();
        if (text[index] === ",") {
          index += 1;
          continue;
        }
        if (text[index] === "]") {
          index += 1;
          return result;
        }
        fail("Expected ',' or ']'");
      }
    }
    if (char === "\"") return parseString();
    if (char === "t") return literal("true", true);
    if (char === "f") return literal("false", false);
    if (char === "n") return literal("null", null);
    const match = /^-?(?:0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/.exec(text.slice(index, index + 400));
    if (!match) fail("Unexpected token");
    index += match[0].length;
    const value = Number(match[0]);
    return (match[1] || match[2]) && Number.isInteger(value) ? new PyFloat(value) : value;
  };
  const parseString = () => {
    const start = index;
    index += 1;
    for (;;) {
      const char = text[index];
      if (char === undefined) fail("Unterminated string");
      if (char === "\\") index += 2;
      else if (char === "\"") break;
      else index += 1;
    }
    index += 1;
    return JSON.parse(text.slice(start, index));
  };
  const result = parseValue();
  space();
  if (index !== text.length) fail("Unexpected trailing data");
  return result;
}
