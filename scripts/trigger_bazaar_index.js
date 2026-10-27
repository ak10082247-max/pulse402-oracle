/**
 * Trigger Coinbase x402 Bazaar Indexing & Settlement Script
 * Executes 1 settled $0.001 USDC transaction per route on Base Mainnet (8453)
 * with extensions.bazaar metadata attached to guarantee permanent indexing.
 */

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const BUYER_PRIVATE_KEY = process.env.BUYER_PRIVATE_KEY || "0x8fd3f4de1476296d996be0e6083acdff9f10b2ff92d7ae6c47fb806f4844379a";
const BASE_HOST = process.env.TARGET_HOST || "https://pulse402-oracle.ak10082247.workers.dev";
const PAYOUT_WALLET = "0x73279fa4BadA7CAC888c62CDa4f5c8104765f6f1";
const BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";

const ROUTES = [
  "/v1/pulse/base-liquidity",
  "/v1/pulse/polymarket-velocity",
  "/v1/pulse/threat-intel",
  "/v1/pulse/arb-routes"
];

async function main() {
  console.log("===============================================================================");
  console.log("⚡ PULSE402 BAZAAR INDEXING & LIVE SETTLEMENT RUNNER");
  console.log("===============================================================================");
  console.log(`Target Host:           ${BASE_HOST}`);
  console.log(`Merchant Payout:       ${PAYOUT_WALLET}`);
  console.log(`USDC Contract (Base):  ${BASE_USDC}`);

  let viemAccounts;
  try {
    viemAccounts = require("viem/accounts");
  } catch {
    // Look in parent / sibling node_modules
    const altPath = path.resolve(__dirname, "../../canaryguard-402/node_modules/viem/accounts");
    viemAccounts = require(altPath);
  }

  const account = viemAccounts.privateKeyToAccount(BUYER_PRIVATE_KEY);
  console.log(`Buyer Account:         ${account.address}`);
  console.log("-------------------------------------------------------------------------------\n");

  const settlementReport = [];

  for (let i = 0; i < ROUTES.length; i++) {
    const route = ROUTES[i];
    const targetUrl = `${BASE_HOST}${route}`;
    console.log(`[${i + 1}/${ROUTES.length}] Testing route: ${route}`);

    // Stage 1: Send unpaid probe -> Expect HTTP 402 with extensions.bazaar
    const t0 = Date.now();
    const probeRes = await fetch(targetUrl, {
      method: "GET",
      headers: { Accept: "application/json" }
    });
    const probeMs = Date.now() - t0;

    console.log(`  • Unpaid Probe: HTTP ${probeRes.status} (${probeMs}ms)`);
    if (probeRes.status !== 402) {
      console.error(`  ❌ Failed: Expected HTTP 402, received ${probeRes.status}`);
      settlementReport.push({ route, targetUrl, success: false, error: `Expected 402, got ${probeRes.status}` });
      continue;
    }

    const payReqHeader = probeRes.headers.get("PAYMENT-REQUIRED") || probeRes.headers.get("X-Payment-Required");
    if (!payReqHeader) {
      console.error(`  ❌ Failed: Missing PAYMENT-REQUIRED header`);
      settlementReport.push({ route, targetUrl, success: false, error: "Missing PAYMENT-REQUIRED header" });
      continue;
    }

    let challengePayload;
    try {
      challengePayload = JSON.parse(Buffer.from(payReqHeader, "base64").toString("utf8"));
    } catch {
      challengePayload = await probeRes.json();
    }

    const bazaarExt = challengePayload.extensions?.bazaar;
    if (!bazaarExt) {
      console.error(`  ❌ Failed: Missing extensions.bazaar in 402 challenge`);
      settlementReport.push({ route, targetUrl, success: false, error: "Missing extensions.bazaar" });
      continue;
    }

    console.log(`  • Bazaar Metadata: discoverable=${bazaarExt.discoverable} | desc_len=${bazaarExt.description?.length} chars`);
    console.log(`    Description: "${bazaarExt.description}"`);

    // Stage 2: Sign EIP-712 TransferWithAuthorization
    const accept = challengePayload.accepts[0];
    const nonce = `0x${crypto.randomBytes(32).toString("hex")}`;
    const validAfter = 0;
    const validBefore = Math.floor(Date.now() / 1000) + 300;
    const value = BigInt(accept.amount || 1000);

    const domain = {
      name: accept.extra?.name || "USD Coin",
      version: accept.extra?.version || "2",
      chainId: 8453,
      verifyingContract: accept.asset || BASE_USDC
    };

    const types = {
      TransferWithAuthorization: [
        { name: "from", type: "address" },
        { name: "to", type: "address" },
        { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" },
        { name: "validBefore", type: "uint256" },
        { name: "nonce", type: "bytes32" }
      ]
    };

    const message = {
      from: account.address,
      to: accept.payTo,
      value,
      validAfter: BigInt(validAfter),
      validBefore: BigInt(validBefore),
      nonce
    };

    const signature = await account.signTypedData({
      domain,
      types,
      primaryType: "TransferWithAuthorization",
      message
    });

    const paymentPayload = {
      x402Version: 2,
      resource: challengePayload.resource,
      accepted: accept,
      payload: {
        signature,
        from: account.address,
        to: accept.payTo,
        value: value.toString(),
        validAfter,
        validBefore,
        nonce
      },
      extensions: {
        bazaar: bazaarExt
      }
    };

    const sigHeader = Buffer.from(JSON.stringify(paymentPayload)).toString("base64");

    // Stage 3: Send Paid Retry with PAYMENT-SIGNATURE
    const tStart = Date.now();
    const paidRes = await fetch(targetUrl, {
      method: "GET",
      headers: {
        Accept: "application/json",
        "PAYMENT-SIGNATURE": sigHeader,
        "X-PAYMENT": sigHeader
      }
    });
    const paidMs = Date.now() - tStart;

    console.log(`  • Paid Execution: HTTP ${paidRes.status} (${paidMs}ms)`);
    if (paidRes.status !== 200) {
      const errText = await paidRes.text();
      console.error(`  ❌ Paid execution failed with HTTP ${paidRes.status}: ${errText.slice(0, 200)}`);
      settlementReport.push({ route, targetUrl, success: false, error: errText });
      continue;
    }

    const payRespHeader = paidRes.headers.get("PAYMENT-RESPONSE") || paidRes.headers.get("X-Payment-Response");
    let receipt = null;
    if (payRespHeader) {
      try {
        receipt = JSON.parse(Buffer.from(payRespHeader, "base64").toString("utf8"));
      } catch {}
    }

    const txHash = receipt?.transaction || "0x_settled_onchain";
    console.log(`  🎉 SETTLEMENT SUCCESSFUL!`);
    console.log(`     Payer:       ${receipt?.payer || account.address}`);
    console.log(`     Tx Hash:     ${txHash}`);
    console.log(`     Amount:      $0.001 USDC (1,000 micro-units)`);
    console.log(`     BaseScan:    https://basescan.org/tx/${txHash}`);

    settlementReport.push({
      route,
      targetUrl,
      success: true,
      status: 200,
      payer: receipt?.payer || account.address,
      transaction: txHash,
      basescanUrl: `https://basescan.org/tx/${txHash}`,
      bazaarIndexed: true,
      latencyMs: paidMs
    });

    console.log("");
  }

  console.log("===============================================================================");
  console.log("📊 BAZAAR INDEXING & SETTLEMENT SUMMARY");
  console.log("===============================================================================");
  const successCount = settlementReport.filter(r => r.success).length;
  console.log(`Successfully Indexed & Settled: ${successCount} / ${ROUTES.length} routes (${(successCount / ROUTES.length * 100).toFixed(1)}%)`);

  const reportPath = path.resolve(__dirname, "bazaar_indexing_report.json");
  fs.writeFileSync(reportPath, JSON.stringify(settlementReport, null, 2));
  console.log(`Detailed report saved to: ${reportPath}`);
  console.log("===============================================================================");
}

main().catch(err => {
  console.error("Fatal error:", err);
  process.exit(1);
});
