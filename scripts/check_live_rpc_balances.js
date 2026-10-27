const BASE_BUYER = "0x6b5f275F028A214fb2e6d21061db791E3b07BEeD";
const BASE_MERCHANT = "0x73279fa4BadA7CAC888c62CDa4f5c8104765f6f1";
const BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";

const SOLANA_BUYER = "ExhRWrmNqLHxzPFxnS2e5X82wZZZ79gYqw9zmNwXSPCQ";
const SOLANA_MERCHANT = "3tQ4GthjUMqNsyrb1rZMocMHoNewFQWjDUvHZqWeYuCN";
const SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";

async function rpcPost(url, method, params) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  return res.json();
}

async function main() {
  console.log("=================================================");
  console.log(" STEP 1: LIVE ON-CHAIN RPC BALANCE AUDIT");
  console.log("=================================================");

  // --- BASE MAINNET ---
  const baseRpc = "https://base-rpc.publicnode.com";
  console.log("\n[Base Mainnet RPC]:", baseRpc);
  
  // Buyer ETH
  const ethRes = await rpcPost(baseRpc, "eth_getBalance", [BASE_BUYER, "latest"]);
  const ethWei = BigInt(ethRes.result || "0x0");
  console.log(`  Base Buyer (${BASE_BUYER}):`);
  console.log(`    ETH Balance:  ${Number(ethWei) / 1e18} ETH`);

  // Buyer USDC (balanceOf)
  const paddedBuyer = BASE_BUYER.slice(2).padStart(64, "0");
  const usdcRes = await rpcPost(baseRpc, "eth_call", [
    { to: BASE_USDC, data: `0x70a08231${paddedBuyer}` },
    "latest",
  ]);
  const usdcMicro = BigInt(usdcRes.result || "0x0");
  console.log(`    USDC Balance: ${Number(usdcMicro) / 1e6} USDC`);

  // Merchant USDC
  const paddedMerchant = BASE_MERCHANT.slice(2).padStart(64, "0");
  const mUsdcRes = await rpcPost(baseRpc, "eth_call", [
    { to: BASE_USDC, data: `0x70a08231${paddedMerchant}` },
    "latest",
  ]);
  const mUsdcMicro = BigInt(mUsdcRes.result || "0x0");
  console.log(`  Base Merchant (${BASE_MERCHANT}):`);
  console.log(`    USDC Balance: ${Number(mUsdcMicro) / 1e6} USDC`);

  // --- SOLANA MAINNET ---
  const solRpc = "https://api.mainnet-beta.solana.com";
  console.log("\n[Solana Mainnet RPC]:", solRpc);

  // Buyer SOL
  const solRes = await rpcPost(solRpc, "getBalance", [SOLANA_BUYER]);
  const solLamports = solRes.result?.value || 0;
  console.log(`  Solana Buyer (${SOLANA_BUYER}):`);
  console.log(`    SOL Balance:  ${solLamports / 1e9} SOL`);

  // Buyer USDC ATAs
  const solBuyerUsdcRes = await rpcPost(solRpc, "getTokenAccountsByOwner", [
    SOLANA_BUYER,
    { mint: SOLANA_USDC },
    { encoding: "jsonParsed" },
  ]);
  let buyerSolUsdc = 0;
  for (const acc of solBuyerUsdcRes.result?.value || []) {
    const amt = acc.account?.data?.parsed?.info?.tokenAmount?.uiAmount || 0;
    buyerSolUsdc += amt;
    console.log(`    ATA (${acc.pubkey}): ${amt} USDC`);
  }
  console.log(`    Total Solana Buyer USDC: ${buyerSolUsdc} USDC`);

  // Merchant USDC ATAs
  const solMerchantUsdcRes = await rpcPost(solRpc, "getTokenAccountsByOwner", [
    SOLANA_MERCHANT,
    { mint: SOLANA_USDC },
    { encoding: "jsonParsed" },
  ]);
  let merchantSolUsdc = 0;
  for (const acc of solMerchantUsdcRes.result?.value || []) {
    const amt = acc.account?.data?.parsed?.info?.tokenAmount?.uiAmount || 0;
    merchantSolUsdc += amt;
    console.log(`    ATA (${acc.pubkey}): ${amt} USDC`);
  }
  console.log(`  Solana Merchant (${SOLANA_MERCHANT}):`);
  console.log(`    Total Solana Merchant USDC: ${merchantSolUsdc} USDC`);
  console.log("\n=================================================");
}

main().catch(console.error);
