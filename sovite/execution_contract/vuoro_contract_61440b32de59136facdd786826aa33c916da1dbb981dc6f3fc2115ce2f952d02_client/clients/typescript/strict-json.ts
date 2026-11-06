/** Strict bounded JSON boundary; no raw input is retained by errors. */
export type JsonValue = null | boolean | number | string | JsonValue[] | {[key: string]: JsonValue};
export type ContractErrorCode = 'json' | 'body_limit' | 'schema_profile' | 'schema_value' | 'manifest' | 'integrity' | 'operation' | 'status' | 'parameter';
export class ContractError extends Error {
  readonly code: ContractErrorCode;
  readonly path: readonly (string | number)[];
  constructor(code: ContractErrorCode, path: readonly (string | number)[] = []) {
    super(code);
    this.name = 'ContractError';
    this.code = code;
    this.path = [...path];
  }
}

const HARD_MAX_BYTES = 4 * 1024 * 1024;
const MAX_DEPTH = 64;
const MAX_NODES = 100_000;
const INTERNAL_FAILURES = new WeakMap<object, ContractErrorCode>();

const TYPED_ARRAY_PROTOTYPE = Object.getPrototypeOf(Uint8Array.prototype);
const TYPED_ARRAY_TAG = Object.getOwnPropertyDescriptor(TYPED_ARRAY_PROTOTYPE, Symbol.toStringTag)?.get;
const TYPED_ARRAY_BYTE_LENGTH = Object.getOwnPropertyDescriptor(TYPED_ARRAY_PROTOTYPE, 'byteLength')?.get;
const TYPED_ARRAY_BYTE_OFFSET = Object.getOwnPropertyDescriptor(TYPED_ARRAY_PROTOTYPE, 'byteOffset')?.get;
const TYPED_ARRAY_BUFFER = Object.getOwnPropertyDescriptor(TYPED_ARRAY_PROTOTYPE, 'buffer')?.get;
const TYPED_ARRAY_SET = Object.getOwnPropertyDescriptor(TYPED_ARRAY_PROTOTYPE, 'set')?.value;

function fail(code: ContractErrorCode = 'json'): never {
  const error = new ContractError(code);
  INTERNAL_FAILURES.set(error, code);
  throw error;
}

function normalizeUnexpected(error: unknown): never {
  if (((typeof error === 'object' && error !== null) || typeof error === 'function') &&
      INTERNAL_FAILURES.has(error as object)) {
    throw new ContractError(INTERNAL_FAILURES.get(error as object) ?? 'json');
  }
  fail('json');
}

class Parser {
  private offset = 0;
  private nodes = 0;

  constructor(private readonly text: string) {}

  parse(): JsonValue {
    this.space();
    const value = this.value(0);
    this.space();
    if (this.offset !== this.text.length) fail();
    return value;
  }

  private node(depth: number): void {
    if (depth > MAX_DEPTH || ++this.nodes > MAX_NODES) fail();
  }

  private space(): void {
    while (this.offset < this.text.length) {
      const c = this.text.charCodeAt(this.offset);
      if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) this.offset++;
      else break;
    }
  }

  private value(depth: number): JsonValue {
    this.node(depth);
    const c = this.text.charCodeAt(this.offset);
    if (c === 0x22) return this.string();
    if (c === 0x7b) return this.object(depth);
    if (c === 0x5b) return this.array(depth);
    if (c === 0x74 && this.take('true')) return true;
    if (c === 0x66 && this.take('false')) return false;
    if (c === 0x6e && this.take('null')) return null;
    if (c === 0x2d || (c >= 0x30 && c <= 0x39)) return this.number();
    return fail();
  }

  private take(literal: string): boolean {
    if (this.text.slice(this.offset, this.offset + literal.length) !== literal) return false;
    this.offset += literal.length;
    return true;
  }

  private hex4(): number {
    if (this.offset + 4 > this.text.length) fail();
    let value = 0;
    for (let i = 0; i < 4; i++) {
      const c = this.text.charCodeAt(this.offset++);
      let digit: number;
      if (c >= 0x30 && c <= 0x39) digit = c - 0x30;
      else if (c >= 0x41 && c <= 0x46) digit = c - 0x41 + 10;
      else if (c >= 0x61 && c <= 0x66) digit = c - 0x61 + 10;
      else return fail();
      value = value * 16 + digit;
    }
    return value;
  }

  private string(): string {
    this.offset++; // opening quote
    let result = '';
    let start = this.offset;
    while (this.offset < this.text.length) {
      const c = this.text.charCodeAt(this.offset);
      if (c === 0x22) {
        result += this.text.slice(start, this.offset);
        this.offset++;
        return result;
      }
      if (c === 0x5c) {
        result += this.text.slice(start, this.offset);
        this.offset++;
        if (this.offset >= this.text.length) fail();
        const escape = this.text.charCodeAt(this.offset++);
        if (escape === 0x22) result += '"';
        else if (escape === 0x5c) result += '\\';
        else if (escape === 0x2f) result += '/';
        else if (escape === 0x62) result += '\b';
        else if (escape === 0x66) result += '\f';
        else if (escape === 0x6e) result += '\n';
        else if (escape === 0x72) result += '\r';
        else if (escape === 0x74) result += '\t';
        else if (escape === 0x75) {
          const first = this.hex4();
          if (first >= 0xd800 && first <= 0xdbff) {
            if (this.text.slice(this.offset, this.offset + 2) !== '\\u') fail();
            this.offset += 2;
            const second = this.hex4();
            if (second < 0xdc00 || second > 0xdfff) fail();
            result += String.fromCharCode(first, second);
          } else {
            if (first >= 0xdc00 && first <= 0xdfff) fail();
            result += String.fromCharCode(first);
          }
        } else fail();
        start = this.offset;
        continue;
      }
      if (c < 0x20) fail();
      if (c >= 0xd800 && c <= 0xdbff) {
        const next = this.text.charCodeAt(this.offset + 1);
        if (!(next >= 0xdc00 && next <= 0xdfff)) fail();
        this.offset += 2;
      } else if (c >= 0xdc00 && c <= 0xdfff) {
        fail();
      } else {
        this.offset++;
      }
    }
    return fail();
  }

  private number(): number {
    const start = this.offset;
    if (this.text.charCodeAt(this.offset) === 0x2d) this.offset++;
    const first = this.text.charCodeAt(this.offset);
    if (first === 0x30) {
      this.offset++;
      const next = this.text.charCodeAt(this.offset);
      if (next >= 0x30 && next <= 0x39) fail();
    } else if (first >= 0x31 && first <= 0x39) {
      this.offset++;
      while (this.digit(this.text.charCodeAt(this.offset))) this.offset++;
    } else fail();
    if (this.text.charCodeAt(this.offset) === 0x2e) {
      this.offset++;
      if (!this.digit(this.text.charCodeAt(this.offset))) fail();
      while (this.digit(this.text.charCodeAt(this.offset))) this.offset++;
    }
    const exponent = this.text.charCodeAt(this.offset);
    if (exponent === 0x65 || exponent === 0x45) {
      this.offset++;
      const sign = this.text.charCodeAt(this.offset);
      if (sign === 0x2b || sign === 0x2d) this.offset++;
      if (!this.digit(this.text.charCodeAt(this.offset))) fail();
      while (this.digit(this.text.charCodeAt(this.offset))) this.offset++;
    }
    const token = this.text.slice(start, this.offset);
    const number = Number(token);
    if (!Number.isFinite(number) || (Number.isInteger(number) && !Number.isSafeInteger(number))) fail();
    return number;
  }

  private digit(c: number): boolean { return c >= 0x30 && c <= 0x39; }

  private object(depth: number): {[key: string]: JsonValue} {
    this.offset++; // opening brace
    this.space();
    const result: {[key: string]: JsonValue} = {};
    const keys = new Set<string>();
    if (this.text.charCodeAt(this.offset) === 0x7d) { this.offset++; return result; }
    while (true) {
      if (this.text.charCodeAt(this.offset) !== 0x22) fail();
      const key = this.string();
      if (keys.has(key)) fail();
      keys.add(key);
      this.space();
      if (this.text.charCodeAt(this.offset++) !== 0x3a) fail();
      this.space();
      const child = this.value(depth + 1);
      Object.defineProperty(result, key, {value: child, enumerable: true, writable: true, configurable: true});
      this.space();
      const delimiter = this.text.charCodeAt(this.offset++);
      if (delimiter === 0x7d) return result;
      if (delimiter !== 0x2c) fail();
      this.space();
    }
  }

  private array(depth: number): JsonValue[] {
    this.offset++; // opening bracket
    this.space();
    const result: JsonValue[] = [];
    if (this.text.charCodeAt(this.offset) === 0x5d) { this.offset++; return result; }
    while (true) {
      result.push(this.value(depth + 1));
      this.space();
      const delimiter = this.text.charCodeAt(this.offset++);
      if (delimiter === 0x5d) return result;
      if (delimiter !== 0x2c) fail();
      this.space();
    }
  }
}

export function strictDecode(raw: Uint8Array, maxBytes: number): JsonValue {
  if (!Number.isFinite(maxBytes) || !Number.isInteger(maxBytes) || maxBytes <= 0 || maxBytes > HARD_MAX_BYTES) {
    fail('body_limit');
  }
  try {
    if (!TYPED_ARRAY_TAG || !TYPED_ARRAY_BYTE_LENGTH || !TYPED_ARRAY_BYTE_OFFSET ||
        !TYPED_ARRAY_BUFFER || !TYPED_ARRAY_SET) fail();
    // Intrinsic typed-array accessors bypass shadowable own properties and reject
    // proxies/DataViews without consulting their prototypes or user getters.
    if (Reflect.apply(TYPED_ARRAY_TAG, raw, []) !== 'Uint8Array') fail();
    const actualLength = Reflect.apply(TYPED_ARRAY_BYTE_LENGTH, raw, []) as number;
    const ownByteLength = Object.getOwnPropertyDescriptor(raw, 'byteLength');
    if (ownByteLength && !('value' in ownByteLength)) fail();
    if (ownByteLength && ownByteLength.value !== actualLength) fail('body_limit');
    if (actualLength > maxBytes || actualLength > HARD_MAX_BYTES) fail('body_limit');
    const actualOffset = Reflect.apply(TYPED_ARRAY_BYTE_OFFSET, raw, []) as number;
    const buffer = Reflect.apply(TYPED_ARRAY_BUFFER, raw, []) as ArrayBufferLike;
    // Copy only this bounded view. Intrinsic set avoids Symbol.species/iterator
    // callbacks and fixes the bytes before decoding a shared or resizable view.
    const snapshot = new Uint8Array(actualLength);
    Reflect.apply(TYPED_ARRAY_SET, snapshot, [new Uint8Array(buffer, actualOffset, actualLength)]);
    if (actualLength >= 3 && snapshot[0] === 0xef && snapshot[1] === 0xbb && snapshot[2] === 0xbf) fail();
    const text = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(snapshot);
    return new Parser(text).parse();
  } catch (error) {
    normalizeUnexpected(error);
  }
}

export function validateJsonValue(value: unknown): JsonValue {
  let nodes = 0;
  let utf8Bytes = 0;
  const active = new WeakSet<object>();
  const addBytes = (count: number): void => {
    if (count > HARD_MAX_BYTES - utf8Bytes) fail('body_limit');
    utf8Bytes += count;
  };
  const stringBytes = (text: string): void => {
    addBytes(2); // JSON quotes
    for (let i = 0; i < text.length; i++) {
      const c = text.charCodeAt(i);
      if (c === 0x22 || c === 0x5c) addBytes(2);
      else if (c < 0x20) {
        addBytes(c === 0x08 || c === 0x09 || c === 0x0a || c === 0x0c || c === 0x0d ? 2 : 6);
      } else if (c >= 0xd800 && c <= 0xdbff) {
        const next = text.charCodeAt(i + 1);
        if (!(next >= 0xdc00 && next <= 0xdfff)) fail();
        addBytes(4);
        i++;
      } else if (c >= 0xdc00 && c <= 0xdfff) fail();
      else if (c <= 0x7f) addBytes(1);
      else if (c <= 0x7ff) addBytes(2);
      else addBytes(3);
    }
  };
  const visit = (item: unknown, depth: number): void => {
    if (depth > MAX_DEPTH || ++nodes > MAX_NODES) fail();
    if (item === null) { addBytes(4); return; }
    if (typeof item === 'boolean') { addBytes(item ? 4 : 5); return; }
    if (typeof item === 'string') { stringBytes(item); return; }
    if (typeof item === 'number') {
      if (!Number.isFinite(item) || (Number.isInteger(item) && !Number.isSafeInteger(item))) fail();
      addBytes(String(item).length); // JSON number spellings are ASCII; String(-0) is the JSON spelling "0".
      return;
    }
    if (typeof item !== 'object') fail();
    const object = item as object;
    if (active.has(object)) fail();
    active.add(object);
    try {
      if (Array.isArray(item)) {
        if (Object.getPrototypeOf(item) !== Array.prototype) fail();
        const keys = Reflect.ownKeys(item);
        if (keys.some(key => typeof key === 'symbol')) fail();
        const lengthDescriptor = Object.getOwnPropertyDescriptor(item, 'length');
        if (!lengthDescriptor || !('value' in lengthDescriptor) ||
            typeof lengthDescriptor.value !== 'number' || lengthDescriptor.value > MAX_NODES) fail();
        const length = lengthDescriptor.value;
        if (keys.length !== length + 1) fail();
        addBytes(2); // brackets
        for (let i = 0; i < length; i++) {
          const descriptor = Object.getOwnPropertyDescriptor(item, String(i));
          if (!descriptor || !descriptor.enumerable || !('value' in descriptor)) fail();
          if (i > 0) addBytes(1);
          visit(descriptor.value, depth + 1);
        }
        return;
      }
      const prototype = Object.getPrototypeOf(item);
      if (prototype !== Object.prototype && prototype !== null) fail();
      const keys = Reflect.ownKeys(item);
      if (keys.some(key => typeof key === 'symbol')) fail();
      const descriptors = Object.getOwnPropertyDescriptors(item);
      addBytes(2); // braces
      let first = true;
      for (const key of keys as string[]) {
        const descriptor = descriptors[key];
        if (!descriptor || !descriptor.enumerable || !('value' in descriptor)) fail();
        if (!first) addBytes(1);
        first = false;
        stringBytes(key);
        addBytes(1); // colon
        visit(descriptor.value, depth + 1);
      }
      return;
    } finally {
      active.delete(object);
    }
  };
  try {
    // Inspect descriptors without invoking getters, copying, or coercing values.
    visit(value, 0);
    return value as JsonValue;
  } catch (error) {
    normalizeUnexpected(error);
  }
}
