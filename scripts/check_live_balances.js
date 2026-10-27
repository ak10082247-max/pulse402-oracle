import { ethers } from "ethers";

const BASE_BUYER_KEY_1 = "0x8fd3f4de1476296d996be0e6083acdff9f10b2ff92d7ae6c47fb806f4844379a";
const SOLANA_BUYER_PUBKEY = "ExhRWrmNqLHxzPFxnS2e5X82wZZZ79gYqw9zmNwXSPCQ";
const SOLANA_MERCHANT = "3tQ4GthjUMqNsyrb1rZMocMHoNewFQWjDUvHZqWeYuCN";
const BASE_MERCHANT = "0x73279fa4BadA7CAC888c62CDa4f5c8104765f6f1";

const BASE_RPC = "https://mainnet.base.org";
const SOLANA_RPC = "https://api.mainnet-beta.solana.com";
const BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";

async function rpcCall(url, method, params = []) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  return res.json();
}

async function check() {
  console.log("=== CHECKING BUYER & MERCHANT WALLET BALANCES ===");

  // 1. Base Wallet
  const wallet = new ethers.Wallet(BASE_BUYER_KEY_1);
  const baseBuyerAddr = wallet.address;
  console.log("\n[Base Buyer Hot Wallet]:", baseBuyerAddr);

  // ETH balance
  const ethRes = await rpcCall(BASE_RPC, "eth_getBalance", [baseBuyerAddr, "latest"]);
  const ethWei = BigInt(ethRes.result || "0x0");
  console.log("  ETH Balance:", Number(ethWei) / 1e18, "ETH");

  // USDC balance: balanceOf(address) -> selector 0x70a08231 + padded address
  const padded = baseBuyerAddr.toLowerCase().replace("0x", "").padStart(64, "0");
  const data = "0x70a08231" + padded;
  const usdcRes = await rpcCall(BASE_RPC, "eth_call", [{ to: BASE_USDC, data }, "latest"]);
  const usdcMicro = BigInt(usdcRes.result || "0x0");
  console.log("  USDC Balance:", Number(usdcMicro) / 1e6, "USDC");

  // Base Merchant
  const mUsdcRes = await rpcCall(BASE_RPC, "eth_call", [
    { to: BASE_USDC, data: "0x70a08231" + BASE_MERCHANT.toLowerCase().replace("0x", "").padStart(64, "0") },
    "latest"
  ]);
  console.log("\n[Base Merchant Wallet]:", BASE_MERCHANT);
  console.log("  USDC Balance:", Number(BigInt(mUsdcRes.result || "0x0")) / 1e6, "USDC");

  // 2. Solana Wallet
  console.log("\n[Solana Buyer Hot Wallet]:", SOLANA_BUYER_PUBKEY);
  const solRes = await rpcCall(SOLANA_RPC, "getBalance", [SOLANA_BUYER_PUBKEY]);
  const lamports = solRes.result?.value || 0;
  console.log("  SOL Balance:", lamports / 1e9, "SOL");

  const solUsdcRes = await rpcCall(SOLANA_RPC, "getTokenAccountsByOwner", [
    SOLANA_BUYER_PUBKEY,
    { mint: SOLANA_USDC },
    { encoding: "jsonParsed" }
  ]);
  const tokenAccounts = solUsdcRes.result?.value || [];
  let totalSolUsdc = 0;
  for (const acc of tokenAccounts) {
    const amt = acc.account?.data?.parsed?.info?.tokenAmount?.uiAmount || 0;
    totalSolUsdc += amt;
    console.log(`  ATA (${acc.pubkey}):`, amt, "USDC");
  }
  console.log("  Total Solana USDC:", totalSolUsdc, "USDC");

  // Solana Merchant
  console.log("\n[Solana Merchant Wallet]:", SOLANA_MERCHANT);
  const mSolUsdcRes = await rpcCall(SOLANA_RPC, "getTokenAccountsByOwner", [
    SOLANA_MERCHANT,
    { mint: SOLANA_USDC },
    { encoding: "jsonParsed" }
  ]);
  const mAccounts = mSolUsdcRes.result?.value || [];
  let mTotalSolUsdc = 0;
  for (const acc of mAccounts) {
    const amt = acc.account?.data?.parsed?.info?.tokenAmount?.uiAmount || 0;
    mTotalSolUsdc += amt;
    console.log(`  ATA (${acc.pubkey}):`, amt, "USDC");
  }
  console.log("  Total Solana Merchant USDC:", mTotalSolUsdc, "USDC");
}

check().catch(console.error);
