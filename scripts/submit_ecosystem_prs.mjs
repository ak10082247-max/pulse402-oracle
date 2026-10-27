import fs from "node:fs";
import path from "node:path";

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

async function submitPrToRepo({
  upstreamOwner,
  repo,
  forkName,
  branchName,
  prTitle,
  prBody,
  insertHeadingPattern,
  entryText,
}) {
  console.log(`\n============================================================`);
  console.log(`Processing PR for: ${upstreamOwner}/${repo} (Fork: ${forkName})`);
  console.log(`============================================================`);

  // 1. Get upstream default branch
  const upRepoRes = await ghFetch(`https://api.github.com/repos/${upstreamOwner}/${repo}`);
  if (!upRepoRes.ok) {
    console.error(`Failed to get upstream repo:`, upRepoRes.data);
    return { repo: `${upstreamOwner}/${repo}`, success: false, error: upRepoRes.data };
  }
  const defaultBranch = upRepoRes.data.default_branch || "main";
  console.log(`Default branch: ${defaultBranch}`);

  // 2. Fork repo
  console.log(`Ensuring fork ${forkName} exists...`);
  const forkRes = await ghFetch(`https://api.github.com/repos/${upstreamOwner}/${repo}/forks`, {
    method: "POST",
  });
  const actualForkName = forkRes.data?.name || forkName;
  console.log(`Actual fork repo name: ${actualForkName}`);

  await sleep(3000);

  // 3. Get latest SHA of default branch on fork
  const refRes = await ghFetch(
    `https://api.github.com/repos/ak10082247-max/${actualForkName}/git/ref/heads/${defaultBranch}`
  );
  if (!refRes.ok) {
    console.error(`Failed to get head ref on fork ${actualForkName}:`, refRes.data);
    return { repo: `${upstreamOwner}/${repo}`, success: false, error: refRes.data };
  }
  const baseSha = refRes.data.object.sha;
  console.log(`Base commit SHA: ${baseSha}`);

  // 4. Create new branch (or use existing)
  const createRefRes = await ghFetch(
    `https://api.github.com/repos/ak10082247-max/${actualForkName}/git/refs`,
    {
      method: "POST",
      body: JSON.stringify({
        ref: `refs/heads/${branchName}`,
        sha: baseSha,
      }),
    }
  );
  if (!createRefRes.ok && createRefRes.status !== 422) {
    console.log(`Create ref response:`, createRefRes.status, createRefRes.data);
  } else {
    console.log(`Branch ${branchName} ready.`);
  }

  // 5. Fetch README from branch
  const fileRes = await ghFetch(
    `https://api.github.com/repos/ak10082247-max/${actualForkName}/contents/README.md?ref=${branchName}`
  );
  if (!fileRes.ok) {
    console.error(`Failed to get README.md:`, fileRes.data);
    return { repo: `${upstreamOwner}/${repo}`, success: false, error: fileRes.data };
  }
  const fileSha = fileRes.data.sha;
  const currentContent = Buffer.from(fileRes.data.content, "base64").toString("utf8");

  // Check if already inserted
  let updatedContent = currentContent;
  if (currentContent.includes("ak10082247-max/pulse402-oracle") || currentContent.includes("Pulse402 Oracle")) {
    console.log(`Pulse402 entry already present in README.md on ${actualForkName}.`);
  } else {
    const lines = currentContent.split("\n");
    const headerIdx = lines.findIndex((l) => insertHeadingPattern.test(l));
    if (headerIdx === -1) {
      console.error(`Could not locate heading matching ${insertHeadingPattern}`);
      return { repo: `${upstreamOwner}/${repo}`, success: false, error: "heading_not_found" };
    }
    console.log(`Found matching heading at line ${headerIdx}: "${lines[headerIdx]}"`);

    // Insert after the description line (first line after heading that starts with text or an empty line)
    let insertLine = headerIdx + 1;
    while (insertLine < lines.length && (lines[insertLine].trim() === "" || (!lines[insertLine].startsWith("- ") && !lines[insertLine].startsWith("* ")))) {
      insertLine++;
    }
    lines.splice(insertLine, 0, entryText);
    updatedContent = lines.join("\n");

    // 6. Update README on branch
    console.log(`Updating README.md on branch ${branchName}...`);
    const updateRes = await ghFetch(
      `https://api.github.com/repos/ak10082247-max/${actualForkName}/contents/README.md`,
      {
        method: "PUT",
        body: JSON.stringify({
          message: `Add Pulse402 Oracle (${prTitle})`,
          content: Buffer.from(updatedContent).toString("base64"),
          sha: fileSha,
          branch: branchName,
        }),
      }
    );
    if (!updateRes.ok) {
      console.error(`Failed to update README.md:`, updateRes.data);
      return { repo: `${upstreamOwner}/${repo}`, success: false, error: updateRes.data };
    }
    console.log(`README.md committed to branch ${branchName}!`);
  }

  // 7. Open Pull Request to upstream
  console.log(`Opening Pull Request to ${upstreamOwner}/${repo}:${defaultBranch}...`);
  const prRes = await ghFetch(
    `https://api.github.com/repos/${upstreamOwner}/${repo}/pulls`,
    {
      method: "POST",
      body: JSON.stringify({
        title: prTitle,
        head: `ak10082247-max:${branchName}`,
        base: defaultBranch,
        body: prBody,
      }),
    }
  );

  if (prRes.ok) {
    const prUrl = prRes.data.html_url;
    console.log(`🎉 PR Created Successfully: ${prUrl}`);
    return { repo: `${upstreamOwner}/${repo}`, success: true, prUrl, number: prRes.data.number };
  } else if (prRes.status === 422 && JSON.stringify(prRes.data).includes("A pull request already exists")) {
    const listPrs = await ghFetch(
      `https://api.github.com/repos/${upstreamOwner}/${repo}/pulls?head=ak10082247-max:${branchName}`
    );
    const existing = (listPrs.data || [])[0];
    const existingUrl = existing?.html_url || `https://github.com/${upstreamOwner}/${repo}/pulls`;
    console.log(`ℹ️ Existing PR already active: ${existingUrl}`);
    return { repo: `${upstreamOwner}/${repo}`, success: true, prUrl: existingUrl, existing: true };
  } else {
    console.error(`Failed to create PR:`, prRes.status, prRes.data);
    return { repo: `${upstreamOwner}/${repo}`, success: false, error: prRes.data };
  }
}

async function main() {
  const existingReport = fs.existsSync("scripts/community_prs_report.json")
    ? JSON.parse(fs.readFileSync("scripts/community_prs_report.json", "utf8"))
    : { results: [] };

  const results = existingReport.results.filter(r => r.success);

  // 1. punkpeye/awesome-mcp-servers
  const r1 = await submitPrToRepo({
    upstreamOwner: "punkpeye",
    repo: "awesome-mcp-servers",
    forkName: "awesome-mcp-servers",
    branchName: "add-pulse402-oracle",
    prTitle: "Add Pulse402 Oracle to Data Platforms",
    prBody: `### Pulse402 Oracle

**Description**: High-frequency consumable x402 data oracle delivering real-time Base DEX liquidity & reserves, Polymarket probability velocity (>2% in 5 min), threat intelligence, and cross-DEX arbitrage routes.

- **Repository**: https://github.com/ak10082247-max/pulse402-oracle
- **Live Gateway**: \`https://pulse402-oracle.ak10082247.workers.dev/mcp\`
- **MCP Registry ID**: \`io.github.ak10082247-max/pulse402-oracle\`
- **Category**: Data Platforms / Financial Intelligence
- **Protocols Supported**: Model Context Protocol (MCP) JSON-RPC 2.0, HTTP 402 / x402 V2 (Base Mainnet \`eip155:8453\` and Solana Mainnet \`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp\`).`,
    insertHeadingPattern: /###\s*.*Data Platforms/i,
    entryText: `- [ak10082247-max/pulse402-oracle](https://github.com/ak10082247-max/pulse402-oracle) 📇 ☁️ - High-frequency consumable x402 data oracle providing Base DEX pool reserves, Polymarket probability velocity, and cross-DEX arbitrage routes for AI agents.`,
  });
  results.push(r1);

  // 2. appcypher/awesome-mcp-servers
  const r2 = await submitPrToRepo({
    upstreamOwner: "appcypher",
    repo: "awesome-mcp-servers",
    forkName: "awesome-mcp-servers-1",
    branchName: "add-pulse402-oracle",
    prTitle: "Add Pulse402 Oracle to Finance MCP Servers",
    prBody: `### Pulse402 Oracle

**Description**: High-frequency consumable x402 data oracle and MCP remote server providing real-time Base DEX pool reserve deltas, Polymarket probability velocity, and cross-DEX arbitrage routes for autonomous agents.

- **Repository**: https://github.com/ak10082247-max/pulse402-oracle
- **MCP Endpoint**: \`https://pulse402-oracle.ak10082247.workers.dev/mcp\`
- **MCP Registry**: \`io.github.ak10082247-max/pulse402-oracle\`
- **License**: MIT`,
    insertHeadingPattern: /##\s*.*Finance/i,
    entryText: `- [Pulse402 Oracle](https://github.com/ak10082247-max/pulse402-oracle) - High-frequency consumable x402 data oracle providing Base DEX liquidity & reserves, Polymarket probability velocity, and cross-DEX arbitrage routes for AI agents.`,
  });
  results.push(r2);

  console.log(`\n============================================================`);
  console.log(`FINAL COMMUNITY PR DISTRIBUTION SUMMARY`);
  console.log(`============================================================`);
  console.log(JSON.stringify(results, null, 2));

  const reportPath = path.resolve("scripts/community_prs_report.json");
  fs.writeFileSync(reportPath, JSON.stringify({ timestamp: new Date().toISOString(), results }, null, 2));
  console.log(`Saved PR report to ${reportPath}`);
}

main().catch(console.error);
