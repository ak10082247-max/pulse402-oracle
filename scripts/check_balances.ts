import { createPublicClient, http, formatUnits, erc20Abi } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { base } from "viem/chains";

const BASE_BUYER_KEY = "0x8fd3f4de1476296d996be0e6083acdff9f10b2ff92d7ae6c47fb806f4844379a";
const SOLANA_BUYER_PUBKEY = "ExhRWrmNqLHxzPFxnS2e5X82wZZZ79gYqw9zmNwXSPCQ";
const BASE_MERCHANT = "0x73279fa4BadA7CAC888c62CDa4f5c8104765f6f1";
const SOLANA_MERCHANT = "3tQ4GthjUMqNsyrb1rZMocMHoNewFQWjDUvHZqWeYuCN";
const SOLANA_MERCHANT_ATA = "3shUSbf9mkGabKqa2pdV7ZrstYvHozwusurLLuugf4dc";

const BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";

async function main() {
  console.log("=== STEP 1: VERIFYING LIVE HOT WALLET & MERCHANT BALANCES ===");

  // 1. Base Mainnet
  const client = createPublicClient({ chain: base, transport: http("https://mainnet.base.org") });
  const baseAccount = privateKeyToAccount(BASE_BUYER_KEY as `0x${string}`);
  console.log("\n[1] Base Buyer Hot Wallet:", baseAccount.address);
  const baseEth = await client.getBalance({ address: baseAccount.address });
  const baseUsdc = await client.readContract({
    address: BASE_USDC,
    abi: erc20Abi,
    functionName: "balanceOf",
    args: [baseAccount.address],
  });
  console.log(`    ETH Balance:  ${formatUnits(baseEth, 18)} ETH`);
  console.log(`    USDC Balance: ${formatUnits(baseUsdc, 6)} USDC`);

  // Base Merchant
  const merchantUsdc = await client.readContract({
    address: BASE_USDC,
    abi: erc20Abi,
    functionName: "balanceOf",
    args: [BASE_MERCHANT as `0x${string}`],
  });
  console.log("\n[2] Base Merchant Wallet:", BASE_MERCHANT);
  console.log(`    USDC Balance: ${formatUnits(merchantUsdc, 6)} USDC`);

  // 2. Solana Mainnet
  console.log("\n[3] Solana Buyer Hot Wallet:", SOLANA_BUYER_PUBKEY);
  const solRpc = "https://api.mainnet-beta.solana.com";
  const solRes = await fetch(solRpc, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getBalance", params: [SOLANA_BUYER_PUBKEY] }),
  });
  const solJson: any = await solRes.json();
  const lamports = solJson?.result?.value || 0;
  console.log(`    SOL Balance:  ${lamports / 1e9} SOL`);

  const solUsdcRes = await fetch(solRpc, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 2,
      method: "getTokenAccountsByOwner",
      params: [SOLANA_BUYER_PUBKEY, { mint: SOLANA_USDC }, { encoding: "jsonParsed" }],
    }),
  });
  const solUsdcJson: any = await solUsdcRes.json();
  const tokenAccounts = solUsdcJson?.result?.value || [];
  let totalSolBuyerUsdc = 0;
  for (const acc of tokenAccounts) {
    const amt = acc.account?.data?.parsed?.info?.tokenAmount?.uiAmount || 0;
    totalSolBuyerUsdc += amt;
    console.log(`    ATA (${acc.pubkey}): ${amt} USDC`);
  }
  console.log(`    Total Solana Buyer USDC: ${totalSolBuyerUsdc} USDC`);

  // Solana Merchant
  console.log("\n[4] Solana Merchant Wallet:", SOLANA_MERCHANT);
  console.log("    Solana Merchant ATA:", SOLANA_MERCHANT_ATA);
  const mRes = await fetch(solRpc, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 3,
      method: "getTokenAccountsByOwner",
      params: [SOLANA_MERCHANT, { mint: SOLANA_USDC }, { encoding: "jsonParsed" }],
    }),
  });
  const mJson: any = await mRes.json();
  const mAccounts = mJson?.result?.value || [];
  let totalSolMerchantUsdc = 0;
  for (const acc of mAccounts) {
    const amt = acc.account?.data?.parsed?.info?.tokenAmount?.uiAmount || 0;
    totalSolMerchantUsdc += amt;
    console.log(`    Merchant ATA (${acc.pubkey}): ${amt} USDC`);
  }
  console.log(`    Total Solana Merchant USDC: ${totalSolMerchantUsdc} USDC`);
}

main().catch(console.error);
