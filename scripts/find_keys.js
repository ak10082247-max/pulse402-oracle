import fs from 'node:fs';
import path from 'node:path';
import { privateKeyToAccount } from 'viem/accounts';

const rootDir = 'C:\\Users\\AK\\Documents\\money maker';
const ignoreDirs = new Set(['node_modules', '.git', '_source_clones', 'dist', 'build', '.next']);

function walk(dir, found = []) {
  try {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const ent of entries) {
      if (ent.isDirectory()) {
        if (!ignoreDirs.has(ent.name)) {
          walk(path.join(dir, ent.name), found);
        }
      } else if (ent.isFile()) {
        if (ent.name.includes('.env') || ent.name.endsWith('.json') || ent.name.endsWith('.js') || ent.name.endsWith('.ts')) {
          found.push(path.join(dir, ent.name));
        }
      }
    }
  } catch (e) {}
  return found;
}

const files = walk(rootDir);
const keyRegex = /0x[0-9a-fA-F]{64}/g;
const accounts = new Map();

for (const file of files) {
  try {
    const text = fs.readFileSync(file, 'utf8');
    const matches = text.match(keyRegex);
    if (matches) {
      for (const m of matches) {
        if (m === '0x0000000000000000000000000000000000000000000000000000000000000000') continue;
        if (m === '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef') continue;
        try {
          const acc = privateKeyToAccount(m);
          if (!accounts.has(acc.address)) {
            accounts.set(acc.address, { key: m, source: file });
          }
        } catch (e) {
          // not a valid private key (probably a hash or topic)
        }
      }
    }
  } catch (e) {}
}

console.log('Found EVM Accounts with Private Keys:');
for (const [addr, info] of accounts.entries()) {
  console.log(`Address: ${addr} | Source: ${info.source}`);
}
