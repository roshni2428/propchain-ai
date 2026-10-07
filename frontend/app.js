// INNOBLOCK 2.0 starter frontend. Settings (network, contract, backend) live in config.js.

const NETWORK = NETWORKS[ACTIVE_NETWORK] || NETWORKS.sepolia;
const CHAIN_ID_HEX = "0x" + NETWORK.chainId.toString(16);

// Read-only connection to the chain: works without a wallet, used for verification
const readProvider = new ethers.JsonRpcProvider(NETWORK.rpcUrl, NETWORK.chainId, { staticNetwork: true });
const readContract = new ethers.Contract(CONTRACT_ADDRESS, CONTRACT_ABI, readProvider);

let signer = null; // set once the wallet is connected

const $ = (id) => document.getElementById(id);

/** Explorer link for a transaction. */
function txUrl(hash) {
  return `${NETWORK.explorer}/tx/${hash}`;
}

/** Show a status line (pending / confirmed / failed), with an explorer link when there is a transaction. */
function setStatus(el, state, message, txHash) {
  el.className = `status ${state}`;
  el.textContent = message;
  if (txHash) {
    const link = document.createElement("a");
    link.href = txUrl(txHash);
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "View transaction ↗";
    el.append(" ", link);
  }
}

/** Turn wallet and ethers errors into messages a user can act on. */
function friendlyError(err) {
  if (err?.code === "ACTION_REJECTED") return "You rejected the request in your wallet.";
  if (err?.code === "INSUFFICIENT_FUNDS") return `Your wallet has no test ${NETWORK.currency}. Get some from a faucet.`;
  if (err?.code === "CALL_EXCEPTION") return "The contract rejected this call. Check the contract address and network in config.js.";
  if (err?.code === "BAD_DATA") return "No contract found at CONTRACT_ADDRESS on this network. Check config.js.";
  if (err?.code === "NETWORK_ERROR") return "Network problem. Check your connection and try again.";
  return err?.shortMessage || err?.message || String(err);
}

/** Call the backend and return its JSON, with a clear message if it can't be reached. */
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(`${BACKEND_URL}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  } catch {
    throw new Error(`Can't reach the backend at ${BACKEND_URL}. Is it running? A sleeping free Render service takes about a minute to wake up.`);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Backend error ${res.status}`);
  return data;
}

/** Ask the wallet to switch to our network, adding the network first if the wallet doesn't know it. */
async function switchNetwork() {
  try {
    await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN_ID_HEX }] });
  } catch (err) {
    const code = err.code ?? err.data?.originalError?.code;
    if (code !== 4902) throw err; // 4902 = this network hasn't been added to the wallet yet
    await window.ethereum.request({
      method: "wallet_addEthereumChain",
      params: [{
        chainId: CHAIN_ID_HEX,
        chainName: NETWORK.name,
        rpcUrls: [NETWORK.rpcUrl],
        blockExplorerUrls: [NETWORK.explorer],
        nativeCurrency: { name: NETWORK.currency, symbol: NETWORK.currency, decimals: 18 },
      }],
    });
  }
}

/** Step 1: connect the wallet, switch it to our network, show address and balance. */
async function connectWallet() {
  const el = $("connect-status");
  if (!window.ethereum) {
    setStatus(el, "failed", "No wallet found. Install MetaMask, or open this page inside the MetaMask mobile app.");
    return;
  }
  try {
    setStatus(el, "pending", "Approve the connection in your wallet…");
    await window.ethereum.request({ method: "eth_requestAccounts" });
    await switchNetwork();
    const provider = new ethers.BrowserProvider(window.ethereum);
    signer = await provider.getSigner();
    const address = await signer.getAddress();
    const balance = await provider.getBalance(address);
    $("wallet-address").textContent = address;
    $("wallet-balance").textContent = `${Number(ethers.formatEther(balance)).toFixed(4)} ${NETWORK.currency}`;
    $("connect-btn").textContent = "Connected";
    setStatus(el, "confirmed", `Connected to ${NETWORK.name}.`);
  } catch (err) {
    setStatus(el, "failed", friendlyError(err));
  }
}

/** Step 2: the USER signs. Hash the text in the browser and store the hash from their wallet. */
async function storeFromWallet() {
  const el = $("wallet-status");
  const text = $("wallet-text").value.trim();
  if (!text) return setStatus(el, "failed", "Type something first.");
  if (!signer) return setStatus(el, "failed", "Connect your wallet first (step 1).");
  try {
    const contract = new ethers.Contract(CONTRACT_ADDRESS, CONTRACT_ABI, signer);
    const hash = ethers.sha256(ethers.toUtf8Bytes(text));
    setStatus(el, "pending", "Confirm the transaction in your wallet…");
    const tx = await contract.store(hash);
    setStatus(el, "pending", "Pending: waiting for the network to confirm…", tx.hash);
    const receipt = await tx.wait(); // throws if the transaction fails
    const event = receipt.logs
      .map((log) => { try { return contract.interface.parseLog(log); } catch { return null; } })
      .find((parsed) => parsed?.name === "RecordStored");
    setStatus(el, "confirmed", `Confirmed: stored as record #${event ? event.args.id : "?"}. Check it in step 4 with the same text.`, tx.hash);
  } catch (err) {
    setStatus(el, "failed", `Failed: ${friendlyError(err)}`, err?.receipt?.hash);
  }
}

/** Step 3a: ask the backend's AI for a decision. */

async function askAI() {
  const el = $("ai-status");

  try {
    const location = $("pf-location")?.value.trim() || "";
    const city = location;
    const locality = "";
    const propertyType = $("pf-type")?.value.trim() || "";
    const area = Number($("pf-area")?.value || 0);
    const bedrooms = Number($("pf-beds")?.value || 0);
    const age = Number($("pf-age")?.value || 0);

    if (!city) {
      return setStatus(el, "failed", "Enter the city first.");
    }

    if (!propertyType) {
      return setStatus(el, "failed", "Choose a property type.");
    }

    if (!area || area <= 0) {
      return setStatus(el, "failed", "Enter a valid property area.");
    }

    setStatus(
      el,
      "pending",
      "Analyzing historical comparable sales…"
    );

    const data = await api("/valuation/ai", {
      method: "POST",
      body: JSON.stringify({
        city,
        locality,
        property_type: propertyType,
        area_sqft: area,
        bedrooms,
        property_age_years: age
      })
    });

    // Keep the original AI decision box working.
    const decision = $("ai-decision");

    const summary = [
      "PROPCHAIN AI PROPERTY VALUATION",
      `City: ${city}`,
      `Locality: ${locality || "Not specified"}`,
      `Property type: ${propertyType}`,
      `Area: ${area} sq ft`,
      `Estimated value: ${data.estimated_value ?? "Not returned"}`,
      `Confidence: ${data.confidence ?? "Not returned"}`,
      `Comparables used: ${data.comparable_properties_used ?? "Not returned"}`,
      `Explanation: ${data.explanation ?? "Not returned"}`
    ].join("\n");

    if (decision) {
      if ("value" in decision) {
        decision.value = summary;
      } else {
        decision.textContent = summary;
      }
    }

    // Keep the valuation available for the existing Store button.
    window.currentValuationRecordText = summary;

    setStatus(
      el,
      "confirmed",
      "Valuation received. Review the result before storing it on-chain."
    );

  } catch (err) {
    setStatus(el, "failed", err.message);
  }
}

/** Step 3b: the BACKEND signs. It hashes the decision, stores the hash and saves the text. */
async function storeDecision() {
  const el = $("ai-status");
  const text = $("ai-decision").value.trim();
  if (!text) return setStatus(el, "failed", "Ask the AI first, or type a decision.");
  setStatus(el, "pending", "Pending: the backend is signing and sending the transaction…");
  try {
    const record = await api("/records", { method: "POST", body: JSON.stringify({ text }) });
    setStatus(el, "confirmed", `Confirmed: stored as record #${record.id}.`, record.txHash);
    loadRecords();
  } catch (err) {
    setStatus(el, "failed", `Failed: ${err.message}`);
  }
}

/** Step 4: re-hash the text in the browser and compare it with the hash stored on-chain. */
async function verifyRecord(id, text, resultEl) {
  resultEl.className = "result";
  resultEl.textContent = "Checking…";
  try {
    const ok = await readContract.verify(id, ethers.sha256(ethers.toUtf8Bytes(text)));
    resultEl.className = `result ${ok ? "ok" : "bad"}`;
    resultEl.textContent = ok ? "✔ Verified on-chain" : "✘ Does not match the chain";
  } catch (err) {
    resultEl.className = "result bad";
    resultEl.textContent = friendlyError(err);
  }
}

/** One row in the records list: editable text, a Verify button, and the transaction link. */
function renderRecord(record) {
  const row = document.createElement("div");
  row.className = "record";
  const id = document.createElement("span");
  id.className = "rid";
  id.textContent = `#${record.id}`;
  const input = document.createElement("input");
  input.value = record.text;
  const button = document.createElement("button");
  button.className = "secondary";
  button.textContent = "Verify";
  const result = document.createElement("span");
  result.className = "result";
  const link = document.createElement("a");
  link.href = txUrl(record.txHash);
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "tx ↗";
  button.onclick = () => verifyRecord(record.id, input.value, result);
  row.append(id, input, button, result, link);
  return row;
}

/** Load the records the backend saved and list them. */
async function loadRecords() {
  const list = $("records");
  list.textContent = "Loading…";
  try {
    const { records } = await api("/records");
    list.textContent = records.length ? "" : "No records yet. Store an AI decision in step 3.";
    records.forEach((record) => list.append(renderRecord(record)));
  } catch (err) {
    list.textContent = err.message;
  }
}

/** Wire up the page and warn about anything left unconfigured in config.js. */
function init() {
  $("network-badge").textContent = NETWORK.name;
  const problems = [];
  if (!NETWORKS[ACTIVE_NETWORK]) problems.push(`ACTIVE_NETWORK "${ACTIVE_NETWORK}" is not in NETWORKS, so Sepolia is used.`);
  if (/^0x0{40}$/.test(CONTRACT_ADDRESS)) problems.push("Set CONTRACT_ADDRESS in frontend/config.js to your deployed contract.");
  if (problems.length) {
    $("setup-warning").textContent = problems.join(" ");
    $("setup-warning").classList.remove("hidden");
  }

  $("connect-btn").onclick = connectWallet;
  $("wallet-store-btn").onclick = storeFromWallet;
  $("ai-ask-btn").onclick = askAI;
  $("ai-store-btn").onclick = storeDecision;
  $("refresh-btn").onclick = loadRecords;
  $("check-btn").onclick = () => verifyRecord(Number($("check-id").value), $("check-text").value, $("check-result"));

  // Reload when the user switches account or network in the wallet, so nothing goes stale
  window.ethereum?.on?.("chainChanged", () => location.reload());
  window.ethereum?.on?.("accountsChanged", () => location.reload());

  loadRecords();
}

init();
