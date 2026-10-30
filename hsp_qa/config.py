"""hsp_qa configuration: every pinned value and limit lives here, in one place.

Nothing in this file is secret. Keys live only in the secrets directory (see wallets.py).
"""
import os
from decimal import Decimal

# Team reporting name used by HSP for post-event aggregation. Used verbatim for create_order.merchant_name,
# the cart's contents.merchant_name and query_payment.expected_merchant_name. Change it ONLY here.
MERCHANT_NAME = "Swift Bot Labs (HSK Meter)"

# Official HSP QA MCP endpoint (streamable HTTP). The client refuses every other host except loopback (mock).
MCP_URL = "https://mcp-qa.hashkeymerchant.com/"
MCP_HOST = "mcp-qa.hashkeymerchant.com"
HEALTH_URL = "https://mcp-qa.hashkeymerchant.com/health"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# The eight tools the guide says must be present before any signing.
REQUIRED_TOOLS = ("get_chain_config", "create_order", "get_order", "prepare_payment", "finalize_payment",
                  "estimate_fee", "submit_payment", "query_payment")

# Money limits (USD). A payment above PER_PAYMENT_CAP_USD or a run/lifetime above TOTAL_CAP_USD is refused.
MIN_PAYMENT_USD = Decimal("0.01")
PER_PAYMENT_CAP_USD = Decimal("1")
TOTAL_CAP_USD = Decimal("5")
DEFAULT_AMOUNT_USD = Decimal("0.01")
# How far the signed token amount may exceed the order's USD amount: price conversion slack (fraction) and,
# when the payer bears gas (gas_fee_advanced false), a fee allowance in USD.
PRICE_SLACK = Decimal("0.02")
FEE_ALLOWANCE_USD = Decimal("0.10")
USD_STABLECOINS = frozenset({"USDC", "USDT", "USDC.E", "USDT0", "USD1", "PYUSD", "FDUSD"})

# Signature validity window: validBefore / deadline must leave time to submit and must not outlive the quote by more
# than this slack. If the server gives no quote_expires_at, validBefore may be at most FALLBACK_MAX_VALID_S ahead.
MIN_TIME_TO_SUBMIT_S = 30
VALID_BEFORE_SLACK_S = 120
FALLBACK_MAX_VALID_S = 600
QUOTE_REPREPARE_MARGIN_S = 20     # re-prepare instead of signing a quote that expires within this many seconds
MAX_PREPARE_ATTEMPTS = 3          # 30043 / expiry -> re-prepare at most this many times
SUBMIT_TIMEOUT_S = 90             # an unanswered submit is treated as uncertain: query, never resubmit

# Polling (guide: every 15 s; report "Pending confirmation" after 10 min and keep the identifiers).
POLL_INTERVAL_S = 15
PENDING_REPORT_S = 600
MAX_WAIT_S = 1800
TERMINAL_STATUSES = frozenset({"payment-safe", "payment-finalized", "payment-failed"})
POLLING_STATUSES = frozenset({"payment-submitted", "payment-verified", "payment-processing", "payment-included",
                              "payment-timeout"})

# Testnets only (QA wallets; never mainnet). Balance/allowance checks use these pinned RPCs, never a URL from the server.
TESTNET_RPC = {
    133: "https://testnet.hsk.xyz",                          # HashKey Chain testnet
    84532: "https://sepolia.base.org",                       # Base Sepolia
    11155111: "https://ethereum-sepolia-rpc.publicnode.com",  # Ethereum Sepolia
    97: "https://bsc-testnet-rpc.publicnode.com",            # BSC testnet
    421614: "https://sepolia-rollup.arbitrum.io/rpc",        # Arbitrum Sepolia
    11155420: "https://sepolia.optimism.io",                 # OP Sepolia
    80002: "https://rpc-amoy.polygon.technology",            # Polygon Amoy
    43113: "https://api.avax-test.network/ext/bc/C/rpc",     # Avalanche Fuji
}
TESTNET_CHAIN_IDS = frozenset(TESTNET_RPC)
CANONICAL_PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

# Probe discipline for the blocked endpoint: at most one live contact per 30 minutes while blocked, and no probes
# after the deadline below.
PROBE_MIN_INTERVAL_S = 30 * 60
PROBE_DEADLINE_UTC = "2026-11-01T14:00:00Z"

# HTTP limits.
HTTP_TIMEOUT_S = 20.0
HTTP_TOTAL_TIMEOUT_S = 60.0
MAX_RESPONSE_BYTES = 2_000_000
USER_AGENT = "hsk-meter-hsp-qa/1.0"

# Paths. HSP_QA_SECRETS_DIR / HSP_QA_STATE_DIR exist so tests can run in a temp dir; production uses the defaults.
HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_DIR = os.environ.get("HSP_QA_SECRETS_DIR", os.path.join(os.path.expanduser("~"), ".hsk-meter", "hsp_qa"))
STATE_DIR = os.environ.get("HSP_QA_STATE_DIR", os.path.join(HERE, "state"))
PAYER_ENV = "hsp_qa_payer.env"
MERCHANT_ENV = "hsp_qa_merchant.env"
RECEIPTS_LIVE = os.path.join(HERE, "receipts.jsonl")
RECEIPTS_MOCK = os.path.join(HERE, "receipts.mock.jsonl")
LIVE_TOOLS_FILE = os.path.join(HERE, "live_tools.json")
LIVE_CHAIN_CONFIG_FILE = os.path.join(HERE, "live_chain_config.json")
PROBE_LOG = os.path.join(HERE, "probe.log")
