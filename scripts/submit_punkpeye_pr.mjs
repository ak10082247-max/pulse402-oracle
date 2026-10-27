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

async function main() {
  console.log("=== SUBMITTING PR TO punkpeye/awesome-mcp-servers ===");
  const upstreamOwner = "punkpeye";
  const repo = "awesome-mcp-servers";
  const branchName = "add-pulse402-oracle";

  // 1. Fetch raw README using raw+json
  const rawRes = await fetch(`https://api.github.com/repos/${upstreamOwner}/${repo}/contents/README.md`, {
    headers: { ...AUTH_HEADER, Accept: "application/vnd.github.raw+json" },
  });
  const rawText = await rawRes.text();
  console.log(`Fetched raw README. Length: ${rawText.length}`);

  // 2. Find insert location in Data Platforms
  const targetHeader = "### 📊 <a name=\"data-platforms\"></a>Data Platforms";
  const targetIdx = rawText.indexOf(targetHeader);
  if (targetIdx === -1) {
    throw new Error("Could not find target header in raw README");
  }
  console.log(`Found target header at index ${targetIdx}`);

  const entry = `- [ak10082247-max/pulse402-oracle](https://github.com/ak10082247-max/pulse402-oracle) 📇 ☁️ - High-frequency consumable x402 data oracle providing Base DEX pool reserves, Polymarket probability velocity, and cross-DEX arbitrage routes for AI agents.\n`;

  // Find the end of the header line
  const nextLineIdx = rawText.indexOf("\n\n", targetIdx);
  const updatedText = rawText.slice(0, nextLineIdx + 2) + entry + rawText.slice(nextLineIdx + 2);
  console.log(`Updated README length: ${updatedText.length}`);

  // 3. Create blob on fork
  console.log("Creating Git Blob on fork...");
  const blobRes = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/blobs`, {
    method: "POST",
    body: JSON.stringify({ content: updatedText, encoding: "utf-8" }),
  });
  if (!blobRes.ok) throw new Error(`Blob creation failed: ${JSON.stringify(blobRes.data)}`);
  const blobSha = blobRes.data.sha;
  console.log(`Blob SHA: ${blobSha}`);

  // 4. Get head commit SHA of default branch
  const refRes = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/ref/heads/main`);
  const parentSha = refRes.data.object.sha;
  console.log(`Parent commit SHA: ${parentSha}`);

  // 5. Create Git Tree
  console.log("Creating Git Tree on fork...");
  const treeRes = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/trees`, {
    method: "POST",
    body: JSON.stringify({
      base_tree: parentSha,
      tree: [{ path: "README.md", mode: "100644", type: "blob", sha: blobSha }],
    }),
  });
  if (!treeRes.ok) throw new Error(`Tree creation failed: ${JSON.stringify(treeRes.data)}`);
  const treeSha = treeRes.data.sha;
  console.log(`Tree SHA: ${treeSha}`);

  // 6. Create Git Commit
  console.log("Creating Git Commit on fork...");
  const commitRes = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/commits`, {
    method: "POST",
    body: JSON.stringify({
      message: "Add Pulse402 Oracle to Data Platforms",
      tree: treeSha,
      parents: [parentSha],
    }),
  });
  if (!commitRes.ok) throw new Error(`Commit creation failed: ${JSON.stringify(commitRes.data)}`);
  const commitSha = commitRes.data.sha;
  console.log(`Commit SHA: ${commitSha}`);

  // 7. Update branch ref (or create if not present)
  console.log(`Updating ref refs/heads/${branchName}...`);
  const updateRefRes = await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/refs/heads/${branchName}`, {
    method: "PATCH",
    body: JSON.stringify({ sha: commitSha, force: true }),
  });
  if (!updateRefRes.ok) {
    // try create
    await ghFetch(`https://api.github.com/repos/ak10082247-max/${repo}/git/refs`, {
      method: "POST",
      body: JSON.stringify({ ref: `refs/heads/${branchName}`, sha: commitSha }),
    });
  }
  console.log(`Branch ${branchName} updated successfully!`);

  // 8. Open Pull Request to upstream
  console.log("Opening Pull Request to punkpeye/awesome-mcp-servers:main...");
  const prRes = await ghFetch(`https://api.github.com/repos/${upstreamOwner}/${repo}/pulls`, {
    method: "POST",
    body: JSON.stringify({
      title: "Add Pulse402 Oracle to Data Platforms",
      head: `ak10082247-max:${branchName}`,
      base: "main",
      body: `### Pulse402 Oracle

**Description**: High-frequency consumable x402 data oracle delivering real-time Base DEX liquidity & reserves, Polymarket probability velocity (>2% in 5 min), threat intelligence, and cross-DEX arbitrage routes.

- **Repository**: https://github.com/ak10082247-max/pulse402-oracle
- **Live Gateway**: \`https://pulse402-oracle.ak10082247.workers.dev/mcp\`
- **MCP Registry ID**: \`io.github.ak10082247-max/pulse402-oracle\`
- **Category**: Data Platforms / Financial Intelligence
- **Protocols Supported**: Model Context Protocol (MCP) JSON-RPC 2.0, HTTP 402 / x402 V2 (Base Mainnet \`eip155:8453\` and Solana Mainnet \`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp\`).`,
    }),
  });

  if (prRes.ok) {
    console.log(`🎉 SUCCESS! PR Created: ${prRes.data.html_url}`);
  } else {
    console.log("PR response:", prRes.status, prRes.data);
  }
}

main().catch(console.error);
