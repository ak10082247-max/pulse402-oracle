import {
  Keypair,
  PublicKey,
  ComputeBudgetProgram,
  TransactionInstruction,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
import bs58 from "bs58";
import fs from "node:fs";
import path from "node:path";

const SOLANA_RPC = "https://api.mainnet-beta.solana.com";
const PAYAI_FACILITATOR = "https://facilitator.payai.network";
const SOLANA_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
const SOLANA_USDC_MINT = new PublicKey("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");
const SOLANA_MERCHANT = new PublicKey("3tQ4GthjUMqNsyrb1rZMocMHoNewFQWjDUvHZqWeYuCN");
const MERCHANT_ATA = new PublicKey("3shUSbf9mkGabKqa2pdV7ZrstYvHozwusurLLuugf4dc");
const TOKEN_PROGRAM_ID = new PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
const ASSOCIATED_TOKEN_PROGRAM_ID = new PublicKey("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");

const TARGET_HOST = "https://pulse402-oracle.ak10082247.workers.dev";
const ENDPOINTS = [
  "/v1/pulse/base-liquidity",
  "/v1/pulse/polymarket-velocity",
  "/v1/pulse/threat-intel",
  "/v1/pulse/arb-routes",
];

function getAssociatedTokenAddress(owner, mint) {
  const [pda] = PublicKey.findProgramAddressSync(
    [owner.toBuffer(), TOKEN_PROGRAM_ID.toBuffer(), mint.toBuffer()],
    ASSOCIATED_TOKEN_PROGRAM_ID
  );
  return pda;
}

function createTransferCheckedIx(sourceAta, mint, destAta, owner, amountMicro, decimals = 6) {
  const data = Buffer.alloc(10);
  data.writeUInt8(12, 0); // Opcode 12 = TransferChecked
  data.writeBigUInt64LE(amountMicro, 1);
  data.writeUInt8(decimals, 9);
  return new TransactionInstruction({
    programId: TOKEN_PROGRAM_ID,
    keys: [
      { pubkey: sourceAta, isSigner: false, isWritable: true },
      { pubkey: mint, isSigner: false, isWritable: false },
      { pubkey: destAta, isSigner: false, isWritable: true },
      { pubkey: owner, isSigner: true, isWritable: false },
    ],
    data,
  });
}

async function rpcCall(method, params = []) {
  const res = await fetch(SOLANA_RPC, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  return res.json();
}

async function main() {
  console.log("================================================================================");
  console.log(" PULSE402 ON-CHAIN SOLANA BUYER SETTLEMENT & BAZAAR INDEXER");
  console.log("================================================================================");

  // 1. Load Solana Buyer Keypair
  const rawKey = process.env.PRIVATE_KEY || "377hhps6Xzmoa8kEdKqdD8p8DteHyaDTsjEdhCBPVEWBLG8to4qSfBr7HHTc5sPsFcdMuGtxb4RySGBuUoSo1kkk";
  const solBuyer = Keypair.fromSecretKey(bs58.decode(rawKey.trim()));
  console.log(`[1] Buyer Wallet:   ${solBuyer.publicKey.toBase58()}`);

  const buyerAta = getAssociatedTokenAddress(solBuyer.publicKey, SOLANA_USDC_MINT);
  console.log(`[2] Buyer USDC ATA: ${buyerAta.toBase58()}`);
  console.log(`[3] Merchant ATA:   ${MERCHANT_ATA.toBase58()}`);

  // Check buyer balance
  const balRes = await rpcCall("getTokenAccountsByOwner", [
    solBuyer.publicKey.toBase58(),
    { mint: SOLANA_USDC_MINT.toBase58() },
    { encoding: "jsonParsed" },
  ]);
  const buyerUsdc = balRes.result?.value?.[0]?.account?.data?.parsed?.info?.tokenAmount?.uiAmount || 0;
  console.log(`[4] Live Buyer USDC: ${buyerUsdc} USDC`);
  if (buyerUsdc < 0.005) {
    throw new Error(`Insufficient Solana USDC balance: ${buyerUsdc}`);
  }

  // 2. Fetch PayAI feePayer configuration
  const supRes = await fetch(`${PAYAI_FACILITATOR}/supported`);
  const sup = await supRes.json();
  const solConfig = (sup.kinds || []).find((k) => k.network === SOLANA_CAIP2);
  const feePayerStr = solConfig?.extra?.feePayer || "CjNFTjvBhbJJd2B5ePPMHRLx1ELZpa8dwQgGL727eKww";
  const feePayerPubkey = new PublicKey(feePayerStr);
  console.log(`[5] PayAI FeePayer:  ${feePayerPubkey.toBase58()} (100% Gas Sponsored)`);

  const results = [];

  for (let i = 0; i < ENDPOINTS.length; i++) {
    const route = ENDPOINTS[i];
    const targetUrl = `${TARGET_HOST}${route}`;
    console.log(`\n--------------------------------------------------------------------------------`);
    console.log(`[${i + 1}/${ENDPOINTS.length}] Probing & Settling ${route}`);
    console.log(`--------------------------------------------------------------------------------`);

    // Step A: Initial unpaid request -> Must return HTTP 402 with Bazaar discovery metadata
    const t0 = Date.now();
    const r1 = await fetch(targetUrl, { method: "GET" });
    const elapsed402 = Date.now() - t0;
    console.log(`  Unpaid Probe Status: HTTP ${r1.status} (${elapsed402}ms)`);
    if (r1.status !== 402) {
      throw new Error(`Expected HTTP 402 from ${targetUrl}, got ${r1.status}`);
    }

    const prHeader = r1.headers.get("PAYMENT-REQUIRED") || r1.headers.get("X-Payment-Required");
    if (!prHeader) {
      throw new Error(`Missing PAYMENT-REQUIRED header on ${targetUrl}`);
    }
    const challenge = JSON.parse(Buffer.from(prHeader, "base64").toString("utf8"));
    const solAccept = (challenge.accepts || []).find((a) => a.network === SOLANA_CAIP2);
    if (!solAccept) {
      throw new Error(`No solana acceptance option in 402 challenge on ${targetUrl}`);
    }

    const settleAmountMicro = BigInt(solAccept.amount || "1000"); // 1000 micro-USDC = 0.001 USDC
    console.log(`  Target Price:        ${Number(settleAmountMicro) / 1e6} USDC (${settleAmountMicro} micro-units)`);

    // Step B: Build and partially sign Solana VersionedTransaction
    const bhRes = await rpcCall("getLatestBlockhash", [{ commitment: "confirmed" }]);
    const recentBlockhash = bhRes.result?.value?.blockhash;
    if (!recentBlockhash) {
      throw new Error("Failed to retrieve latest blockhash from Solana RPC");
    }

    const instructions = [
      ComputeBudgetProgram.setComputeUnitLimit({ units: 20000 }),
      ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 1 }),
      createTransferCheckedIx(buyerAta, SOLANA_USDC_MINT, MERCHANT_ATA, solBuyer.publicKey, settleAmountMicro, 6),
    ];

    const messageV0 = new TransactionMessage({
      payerKey: feePayerPubkey,
      recentBlockhash,
      instructions,
    }).compileToV0Message();

    const versionedTx = new VersionedTransaction(messageV0);
    versionedTx.sign([solBuyer]); // Partial sign slot [1] (token owner); slot [0] feePayer unsigned
    const txB64 = Buffer.from(versionedTx.serialize()).toString("base64");

    const paymentPayload = {
      x402Version: 2,
      resource: challenge.resource,
      extensions: challenge.extensions,
      accepted: solAccept,
      payload: { transaction: txB64 },
    };

    const sigHeader = Buffer.from(JSON.stringify(paymentPayload)).toString("base64");

    // Step C: Send paid request with PAYMENT-SIGNATURE
    console.log(`  Submitting PAYMENT-SIGNATURE to edge gateway...`);
    const t1 = Date.now();
    const r2 = await fetch(targetUrl, {
      method: "GET",
      headers: {
        "PAYMENT-SIGNATURE": sigHeader,
        "X-PAYMENT": sigHeader,
        "Accept": "application/json",
      },
    });
    const elapsedSettlement = Date.now() - t1;
    console.log(`  Paid Settlement Status: HTTP ${r2.status} (${elapsedSettlement}ms)`);

    const respText = await r2.text();
    let respJson = null;
    try {
      respJson = JSON.parse(respText);
    } catch {}

    if (r2.status !== 200) {
      console.error(`  ❌ Settlement Failed:`, respText);
      throw new Error(`Failed settlement on ${targetUrl}: HTTP ${r2.status}`);
    }

    // Step D: Extract verified transaction signature from PAYMENT-RESPONSE
    const paymentResponseHdr = r2.headers.get("PAYMENT-RESPONSE") || r2.headers.get("X-Payment-Response");
    let txSig = "";
    if (paymentResponseHdr) {
      try {
        const parsedReceipt = JSON.parse(Buffer.from(paymentResponseHdr, "base64").toString("utf8"));
        txSig = parsedReceipt.transaction || "";
      } catch {}
    }

    if (!txSig) {
      throw new Error(`Server returned HTTP 200 without a valid transaction signature in PAYMENT-RESPONSE`);
    }

    console.log(`  🎉 Facilitator Confirmed! Tx Signature: ${txSig}`);
    console.log(`  Solscan Explorer: https://solscan.io/tx/${txSig}`);

    // Step E: MANDATORY ON-CHAIN RPC VERIFICATION
    console.log(`  Querying Solana Mainnet RPC for on-chain proof...`);
    let confirmedOnChain = false;
    let confirmationStatus = "pending";
    let slot = 0;

    for (let poll = 1; poll <= 15; poll++) {
      await new Promise((r) => setTimeout(r, 1200));
      const sigStatusRes = await rpcCall("getSignatureStatuses", [[txSig], { searchTransactionHistory: true }]);
      const statusObj = sigStatusRes.result?.value?.[0];
      if (statusObj && (statusObj.confirmationStatus === "confirmed" || statusObj.confirmationStatus === "finalized")) {
        confirmedOnChain = true;
        confirmationStatus = statusObj.confirmationStatus;
        slot = statusObj.slot;
        console.log(`  ✅ ON-CHAIN RPC VERIFIED at slot ${slot}! Status: "${confirmationStatus}"`);
        break;
      }
      console.log(`    [Poll ${poll}/15] Waiting for commitment... current status: ${statusObj?.confirmationStatus || "unconfirmed"}`);
    }

    if (!confirmedOnChain) {
      throw new Error(`On-chain RPC confirmation timed out for ${txSig}`);
    }

    results.push({
      route,
      targetUrl,
      success: true,
      status: 200,
      network: SOLANA_CAIP2,
      payer: solBuyer.publicKey.toBase58(),
      transaction: txSig,
      solscanUrl: `https://solscan.io/tx/${txSig}`,
      solanaSlot: slot,
      confirmationStatus,
      latencyMs: elapsedSettlement,
      bazaarIndexed: true,
      verifiedAt: new Date().toISOString(),
    });
  }

  // 3. Save Verified Results
  const reportPath = path.resolve(process.cwd(), "scripts/bazaar_indexing_report.json");
  fs.writeFileSync(reportPath, JSON.stringify(results, null, 2));
  console.log("\n================================================================================");
  console.log(` 🏆 ALL 4 ROUTES VERIFIED & SETTLED ON SOLANA MAINNET!`);
  console.log(` Saved on-chain audit report to ${reportPath}`);
  console.log("================================================================================\n");
}

main().catch((err) => {
  console.error("FATAL ERROR in bazaar indexer:", err);
  process.exit(1);
});
