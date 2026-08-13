'use strict';

const fs = require('node:fs');
const path = require('node:path');

const PROJECT_ROOT = path.resolve(__dirname, '..', '..');

function readProjectFile(relative) {
  return fs.readFileSync(path.join(PROJECT_ROOT, relative), 'utf8');
}

function rustTokens(source) {
  const tokens = [];
  let index = 0;
  while (index < source.length) {
    const rest = source.slice(index);
    const ignored = rest.match(/^(?:\s+|\/\/[^\n]*(?:\n|$)|\/\*[\s\S]*?\*\/|r#*"[\s\S]*?"#*|"(?:\\.|[^"\\])*")/);
    if (ignored) {
      index += ignored[0].length;
      continue;
    }
    const token = rest.match(/^[A-Za-z_][A-Za-z0-9_]*|^::|^->|^./s);
    tokens.push(token[0]);
    index += token[0].length;
  }
  return tokens;
}

function balancedItem(tokens, signature) {
  const start = tokens.findIndex((_, index) => signature.every((token, offset) => tokens[index + offset] === token));
  if (start < 0) throw new Error(`Rust item not found: ${signature.join(' ')}`);
  const open = tokens.indexOf('{', start + signature.length);
  if (open < 0) throw new Error(`Rust item has no body: ${signature.join(' ')}`);
  let depth = 0;
  for (let index = open; index < tokens.length; index += 1) {
    if (tokens[index] === '{') depth += 1;
    if (tokens[index] === '}') depth -= 1;
    if (depth === 0) return tokens.slice(start, index + 1);
  }
  throw new Error(`Rust item is unbalanced: ${signature.join(' ')}`);
}

function fieldType(itemTokens, fieldName) {
  const field = itemTokens.findIndex((token, index) => token === fieldName && itemTokens[index + 1] === ':');
  if (field < 0) throw new Error(`Rust field not found: ${fieldName}`);
  const end = itemTokens.indexOf(',', field + 2);
  return itemTokens.slice(field + 2, end).join('');
}

function hasSequence(tokens, sequence) {
  return tokens.some((_, index) => sequence.every((token, offset) => tokens[index + offset] === token));
}

function rustConst(source, name) {
  const match = source.match(new RegExp(`const\\s+${name}\\s*:\\s*&str\\s*=\\s*"([^"]*)"\\s*;`));
  if (!match) throw new Error(`Rust string constant not found: ${name}`);
  return match[1];
}

function listJavaScriptFiles(root, current = root) {
  const files = [];
  for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
    const absolute = path.join(current, entry.name);
    if (entry.isDirectory()) files.push(...listJavaScriptFiles(root, absolute));
    if (entry.isFile() && entry.name.endsWith('.js')) files.push(path.relative(root, absolute));
  }
  return files.sort();
}

module.exports = {
  PROJECT_ROOT,
  balancedItem,
  fieldType,
  hasSequence,
  listJavaScriptFiles,
  readProjectFile,
  rustConst,
  rustTokens,
};
