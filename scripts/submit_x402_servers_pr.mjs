const GITHUB_PAT = process.env.GITHUB_PAT || "";
const AUTH_HEADER = {
  Authorization: `Bearer ${GITHUB_PAT}`,
  "User-Agent": "Pulse402-Automated-Submitter",
  Accept: "application/vnd.github+json",
};

async function ghFetch(url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: { ...AUTH_HEADER, ...(options.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  return { status: res.status, ok: res.ok, data };
}

async function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function main() {
  console.log("=== SUBMITTING PR TO fffilimonov/awesome-x402-servers ===");
  const upstreamOwner = "fffilimonov";
  const repo = "awesome-x402-servers";
  const branchName = "add-pulse402-oracle";

  // 1. Fork repo
  console.log(`Forking ${upstreamOwner}/${repo}...`);
  const forkRes = await ghFetch(`https://api.github.com/repos/${upstreamOwner}/${repo}/forks`, {
    method: "POST",
  });
  console.log(`Fork status: ${forkRes.status} (name: ${forkRes.data.name})`);

  let forkReady = false;
  for (let i = 0; i < 15; i++) {
    await sleep(2000);
    const checkFork = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}`);
    if (checkFork.ok) {
      forkReady = true;
      break;
    }
  }
  if (!forkReady) throw new Error("Fork not ready");

  // 2. Fetch raw README
  const rawRes = await fetch(`https://api.github.com/repos/${upstreamOwner}/${repo}/contents/README.md`, {
    headers: { ...AUTH_HEADER, Accept: "application/vnd.github.raw+json" },
  });
  const rawText = await rawRes.text();

  // 3. Insert under Community Servers
  const targetHeader = "## 📁 Community Servers";
  const targetIdx = rawText.indexOf(targetHeader);
  if (targetIdx === -1) throw new Error("Header not found");

  const entry = `* Pulse402 - High-frequency consumable x402 data oracle delivering real-time Base DEX liquidity, Polymarket probability velocity, and cross-DEX arbitrage routes for AI agents. [Website](https://pulse402-oracle.ak10082247.workers.dev) [Github](https://github.com/ak10082247-max/pulse402-oracle)\n`;

  const nextLineIdx = rawText.indexOf("\n\n", targetIdx);
  const updatedText = rawText.slice(0, nextLineIdx + 2) + entry + rawText.slice(nextLineIdx + 2);

  // 4. Create blob on fork
  console.log("Creating Git Blob on fork...");
  const blobRes = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/blobs`, {
    method: "POST",
    body: JSON.stringify({ content: updatedText, encoding: "utf-8" }),
  });
  const blobSha = blobRes.data.sha;

  // 5. Get default branch head SHA
  const refRes = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/ref/heads/main`);
  const parentSha = refRes.data.object.sha;

  // 6. Create Tree & Commit
  console.log("Creating Git Tree & Commit...");
  const treeRes = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/trees`, {
    method: "POST",
    body: JSON.stringify({
      base_tree: parentSha,
      tree: [{ path: "README.md", mode: "100644", type: "blob", sha: blobSha }],
    }),
  });
  const treeSha = treeRes.data.sha;

  const commitRes = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/commits`, {
    method: "POST",
    body: JSON.stringify({
      message: "Add Pulse402 Oracle to Community Servers",
      tree: treeSha,
      parents: [parentSha],
    }),
  });
  const commitSha = commitRes.data.sha;

  // 7. Update branch ref
  console.log(`Updating ref refs/heads/${branchName}...`);
  await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/refs/heads/${branchName}`, {
    method: "PATCH",
    body: JSON.stringify({ sha: commitSha, force: true }),
  }).then(async (r) => {
    if (!r.ok) {
      await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/refs`, {
        method: "POST",
        body: JSON.stringify({ ref: `refs/heads/${branchName}`, sha: commitSha }),
      });
    }
  });

  // 8. Open Pull Request to upstream
  console.log(`Opening Pull Request to ${upstreamOwner}/${repo}:main...`);
  const prRes = await ghFetch(`https://api.github.com/repos/${upstreamOwner}/${repo}/pulls`, {
    method: "POST",
    body: JSON.stringify({
      title: "Add Pulse402 Oracle to Community Servers",
      head: `ak10082247-max:${branchName}`,
      base: "main",
      body: `### Pulse402 Oracle

**Description**: High-frequency consumable x402 data oracle delivering real-time Base DEX liquidity (Aerodrome/Uniswap v3), Polymarket probability velocity shifts (>2% in 5 min), threat intelligence, and cross-DEX arbitrage routes.

- **Website**: https://pulse402-oracle.ak10082247.workers.dev
- **Repository**: https://github.com/ak10082247-max/pulse402-oracle
- **MCP Endpoint**: https://pulse402-oracle.ak10082247.workers.dev/mcp
- **Supported Chains**: Base Mainnet (\`eip155:8453\`) & Solana Mainnet (\`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp\`)
- **Price**: $0.001 USDC`,
    }),
  });

  if (prRes.ok) {
    console.log(`🎉 SUCCESS! PR Created: ${prRes.data.html_url}`);
    return prRes.data.html_url;
  } else {
    console.log("PR response:", prRes.status, prRes.data);
    return null;
  }
}

main().catch(console.error);
