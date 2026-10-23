// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test, console} from "forge-std/Test.sol";
import {StdInvariant} from "forge-std/StdInvariant.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {HskPayPerRequest} from "../src/HskPayPerRequest.sol";
import {FiatTokenFixture, IFiatToken} from "./Base.sol";

/// @dev Stateful handler with an independent model of every rule, run against the real FiatTokenV2_2 code. Each call
///      predicts success or the EXACT revert data (contract errors with parameters, token error strings) and compares
///      it with what happened; any difference counts as a mismatch. Both pay paths (approve + pay, signed EIP-3009
///      authorization submitted by a third party) are exercised. Ghosts: successful pays/refunds per requestId, exact
///      token deltas of every movement, tokens sent to the contract without credit.
contract PaywallHandler is Test {
    HskPayPerRequest public immutable c;
    IFiatToken public immutable usdc;
    IERC20 internal immutable t;
    address internal blacklister;
    address[2] internal providers;
    uint256[3] internal payerPks;
    address[3] internal payers; // payers[2] gets blacklisted / unblacklisted by toggleBlacklist()
    address internal outsider;
    address internal payeeA;
    address internal payeeB;
    address internal blPayee; // a payee that is blacklisted (can be credited, must withdraw elsewhere)
    address internal blSink; // another blacklisted address
    address[5] public creditHolders; // every address that can ever hold credit

    uint256 internal constant MAX_ENDPOINTS = 6;

    struct E {
        address provider;
        address payee;
        uint128 price;
        bool active;
        uint64 paidCount;
        uint128 totalPaid;
        uint128 totalRefunded;
        uint64 refundCount;
    }

    struct P {
        address payer;
        uint256 endpointId;
        uint128 amount;
        bool refunded;
    }

    mapping(uint256 => E) internal m;
    uint256 public count;
    mapping(bytes32 => P) internal pm;
    bytes32[33] public pool; // candidate requestIds: 4 payer prefixes x 8 salts + bytes32(0)
    mapping(address => uint256) public mCredit;
    mapping(address => mapping(address => bool)) public mApproved; // payee => provider
    mapping(address => bool) public mBlacklisted;
    uint256 public mPaid;
    uint256 public mRefunded;
    uint256 public mWithdrawn;
    uint256 public mUncredited; // donations + withdrawals to the contract itself

    // ghosts
    mapping(bytes32 => uint256) public ghostPays;
    mapping(bytes32 => uint256) public ghostRefunds;
    uint256 public mismatches;
    uint256 public badTransfers;
    string public lastMismatch;
    mapping(string => uint256) public calls;
    string[] internal callNames;

    bytes internal constant BL_ERR =
        abi.encodeWithSignature("Error(string)", "Blacklistable: account is blacklisted");
    bytes internal constant SIG_ERR = abi.encodeWithSignature("Error(string)", "FiatTokenV2: invalid signature");

    constructor(HskPayPerRequest c_, IFiatToken usdc_, address minter_, address blacklister_) {
        c = c_;
        usdc = usdc_;
        t = IERC20(address(usdc_));
        blacklister = blacklister_;
        providers = [makeAddr("providerA"), makeAddr("providerB")];
        payerPks = [uint256(0xA1), uint256(0xA2), uint256(0xA3)];
        for (uint256 i = 0; i < 3; i++) payers[i] = vm.addr(payerPks[i]);
        outsider = makeAddr("outsider");
        payeeA = makeAddr("payeeA");
        payeeB = makeAddr("payeeB");
        blPayee = makeAddr("blPayee");
        blSink = makeAddr("blSink");
        creditHolders = [payeeA, payeeB, blPayee, providers[0], providers[1]];
        // consent: payeeA approves both providers, blPayee approves provider A only, payeeB nobody (yet)
        for (uint256 i = 0; i < 2; i++) {
            vm.prank(payeeA);
            c_.approveProvider(providers[i], true);
            mApproved[payeeA][providers[i]] = true;
        }
        vm.prank(blPayee);
        c_.approveProvider(providers[0], true);
        mApproved[blPayee][providers[0]] = true;
        address[4] memory funded = [payers[0], payers[1], payers[2], outsider];
        for (uint256 i = 0; i < 4; i++) {
            vm.prank(minter_);
            usdc_.mint(funded[i], 1_000_000_000e6);
            vm.prank(funded[i]);
            t.approve(address(c_), type(uint256).max);
        }
        vm.startPrank(blacklister_);
        usdc_.blacklist(blPayee);
        usdc_.blacklist(blSink);
        vm.stopPrank();
        mBlacklisted[blPayee] = true;
        mBlacklisted[blSink] = true;
        address[4] memory prefixes = [payers[0], payers[1], payers[2], address(0)];
        for (uint256 p = 0; p < 4; p++) {
            for (uint256 s = 0; s < 8; s++) {
                pool[p * 8 + s] = c_.requestIdFor(prefixes[p], keccak256(abi.encode("salt", s)));
            }
        }
        pool[32] = bytes32(0);
        string[25] memory names = ["approve", "approve_rej", "register", "register_rej", "setPrice", "setPrice_rej",
            "setActive", "setActive_rej", "pay_ok", "pay_rej", "auth_ok", "auth_rej", "refund_ok", "refund_rej",
            "withdraw_ok", "withdraw_rej", "warp", "rej_notForPayer", "rej_alreadyPaid", "rej_wrongAmount",
            "rej_blacklisted", "rej_insufficientCredit", "rej_badSignature", "donate", "blacklist_toggle"];
        for (uint256 i = 0; i < names.length; i++) callNames.push(names[i]);
    }

    // ------------------------------------------------------------------ helpers
    function _h(uint256 x, string memory tag) internal pure returns (uint256) {
        return uint256(keccak256(abi.encode(x, tag)));
    }

    function _pickEndpoint(uint256 seed) internal view returns (uint256) {
        seed = _h(seed, "ep");
        if (count == 0 || seed % 12 == 0) return seed % 2 == 0 ? 0 : count + 1;
        return 1 + (seed % count);
    }

    function _price(uint256 seed) internal pure returns (uint128) {
        uint256 s = _h(seed, "price");
        uint256 k = s % 6;
        if (k == 0) return 0;
        if (k == 1) return 1;
        if (k == 2) return 10_000; // 0.01 USDC.e
        if (k == 3) return 50_000;
        return uint128(1 + (s >> 8) % 1_000e6);
    }

    function _compare(string memory what, bool ok, bytes memory ret, bytes memory expected) internal {
        bool expectOk = expected.length == 0;
        if (ok != expectOk || (!ok && keccak256(ret) != keccak256(expected))) {
            mismatches++;
            lastMismatch = what;
            console.log("MISMATCH", what);
            console.logBytes(ret);
            console.logBytes(expected);
        }
    }

    function _sign(uint256 pk, uint256 value, uint256 validBefore, bytes32 nonce) internal view returns (bytes memory) {
        address from = vm.addr(pk);
        bytes32 sh = keccak256(
            abi.encode(usdc.RECEIVE_WITH_AUTHORIZATION_TYPEHASH(), from, address(c), value, 0, validBefore, nonce)
        );
        (uint8 v, bytes32 r, bytes32 s) =
            vm.sign(pk, keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), sh)));
        return abi.encodePacked(r, s, v);
    }

    // ------------------------------------------------------------------ actions
    function register(uint256 seed) external {
        if (count >= MAX_ENDPOINTS && seed % 4 != 0) return;
        address provider = providers[seed % 2];
        uint256 s = _h(seed, "payee");
        address[6] memory payeeChoices = [payeeA, payeeB, blPayee, provider, address(0), address(c)];
        address payee = payeeChoices[s % 8 < 6 ? s % 8 : s % 4];
        uint128 price = _price(seed);
        uint256 nk = _h(seed, "name") % 10;
        string memory name = nk == 0 ? "" : (nk == 1
            ? "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" // 65 bytes
            : "GET /v1/weather");
        bytes memory expected;
        if (payee == address(0)) expected = abi.encodeWithSelector(HskPayPerRequest.ZeroAddress.selector);
        else if (payee == address(c)) expected = abi.encodeWithSelector(HskPayPerRequest.InvalidPayee.selector);
        else if (payee != provider && !mApproved[payee][provider]) {
            expected = abi.encodeWithSelector(HskPayPerRequest.PayeeNotApproved.selector, payee);
        } else if (price == 0) expected = abi.encodeWithSelector(HskPayPerRequest.InvalidPrice.selector);
        else if (nk <= 1) expected = abi.encodeWithSelector(HskPayPerRequest.InvalidName.selector);
        vm.prank(provider);
        (bool ok, bytes memory ret) = address(c).call(abi.encodeCall(c.registerEndpoint, (price, payee, name)));
        _compare("register", ok, ret, expected);
        if (ok && expected.length == 0) {
            count++;
            if (abi.decode(ret, (uint256)) != count) {
                mismatches++;
                lastMismatch = "register id";
            }
            m[count] = E(provider, payee, price, true, 0, 0, 0, 0);
            calls["register"]++;
        } else {
            calls["register_rej"]++;
        }
    }

    function approve(uint256 seed) external {
        address[3] memory payeesList = [payeeA, payeeB, blPayee];
        address payee = payeesList[_h(seed, "who") % 3];
        uint256 ps = _h(seed, "prov") % 9;
        address provider = ps == 8 ? address(0) : providers[ps % 2];
        bool approved = _h(seed, "ok") % 3 != 0;
        bytes memory expected;
        if (provider == address(0)) expected = abi.encodeWithSelector(HskPayPerRequest.ZeroAddress.selector);
        vm.prank(payee);
        (bool ok, bytes memory ret) = address(c).call(abi.encodeCall(c.approveProvider, (provider, approved)));
        _compare("approve", ok, ret, expected);
        if (ok && expected.length == 0) {
            mApproved[payee][provider] = approved;
            calls["approve"]++;
        } else {
            calls["approve_rej"]++;
        }
    }

    function _providerErr(uint256 id, address caller) internal view returns (bytes memory) {
        if (id == 0 || id > count) return abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, id);
        if (caller != m[id].provider) return abi.encodeWithSelector(HskPayPerRequest.NotProvider.selector);
        return "";
    }

    function setPrice(uint256 seed) external {
        uint256 id = _pickEndpoint(seed);
        address caller = _h(seed, "caller") % 5 == 0 ? outsider : (id >= 1 && id <= count ? m[id].provider : providers[0]);
        uint128 price = _price(_h(seed, "newprice"));
        bytes memory expected = _providerErr(id, caller);
        if (expected.length == 0 && price == 0) expected = abi.encodeWithSelector(HskPayPerRequest.InvalidPrice.selector);
        vm.prank(caller);
        (bool ok, bytes memory ret) = address(c).call(abi.encodeCall(c.setPrice, (id, price)));
        _compare("setPrice", ok, ret, expected);
        if (ok && expected.length == 0) {
            m[id].price = price;
            calls["setPrice"]++;
        } else {
            calls["setPrice_rej"]++;
        }
    }

    function setActive(uint256 seed) external {
        bool active = _h(seed, "on") % 4 != 0; // mostly (re)activate, so payments keep flowing
        uint256 id = _pickEndpoint(seed);
        address caller = _h(seed, "caller") % 5 == 0 ? payeeA : (id >= 1 && id <= count ? m[id].provider : providers[1]);
        bytes memory expected = _providerErr(id, caller);
        vm.prank(caller);
        (bool ok, bytes memory ret) = address(c).call(abi.encodeCall(c.setActive, (id, active)));
        _compare("setActive", ok, ret, expected);
        if (ok && expected.length == 0) {
            m[id].active = active;
            calls["setActive"]++;
        } else {
            calls["setActive_rej"]++;
        }
    }

    function _payExpected(uint256 id, bytes32 rid, address payer, uint256 amount, uint256 price)
        internal
        returns (bytes memory)
    {
        address bound = address(uint160(uint256(rid) >> 96));
        if (id == 0 || id > count) return abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, id);
        if (!m[id].active) return abi.encodeWithSelector(HskPayPerRequest.EndpointInactive.selector, id);
        if (rid == bytes32(0)) return abi.encodeWithSelector(HskPayPerRequest.ZeroRequestId.selector);
        if (bound != address(0) && bound != payer) {
            calls["rej_notForPayer"]++;
            return abi.encodeWithSelector(HskPayPerRequest.RequestNotForPayer.selector, bound);
        }
        if (pm[rid].payer != address(0)) {
            calls["rej_alreadyPaid"]++;
            return abi.encodeWithSelector(HskPayPerRequest.AlreadyPaid.selector, rid);
        }
        if (amount != price) {
            calls["rej_wrongAmount"]++;
            return abi.encodeWithSelector(HskPayPerRequest.WrongAmount.selector, amount, price);
        }
        if (mBlacklisted[payer]) {
            calls["rej_blacklisted"]++;
            return BL_ERR;
        }
        return "";
    }

    function _recordPay(uint256 id, bytes32 rid, address payer, uint256 amount) internal {
        pm[rid] = P(payer, id, uint128(amount), false);
        ghostPays[rid]++;
        E storage e = m[id];
        e.paidCount++;
        e.totalPaid += uint128(amount);
        mCredit[e.payee] += amount;
        mPaid += amount;
    }

    struct PayCall {
        uint256 id;
        bytes32 rid;
        uint256 payerIdx; // 0..2 = payers, 3 = outsider (approve path only)
        address payer;
        uint256 amount;
        uint256 price;
    }

    function _payArgs(uint256 seed) internal view returns (PayCall memory q) {
        q.id = _pickEndpoint(seed);
        q.rid = pool[_h(seed, "rid") % 33];
        uint256 ps = _h(seed, "payer") % 8;
        address bound = address(uint160(uint256(q.rid) >> 96));
        if (bound != address(0) && ps >= 2) {
            q.payer = bound; // mostly the right payer
            q.payerIdx = bound == payers[0] ? 0 : (bound == payers[1] ? 1 : 2);
        } else {
            q.payerIdx = ps % 4;
            q.payer = q.payerIdx == 3 ? outsider : payers[q.payerIdx];
        }
        q.price = (q.id >= 1 && q.id <= count) ? m[q.id].price : 0;
        uint256 ak = _h(seed, "amount") % 8;
        q.amount = ak == 0 ? 0 : (ak == 6 ? q.price + 1 : (ak == 7 ? (q.price > 0 ? q.price - 1 : 1) : q.price));
    }

    function _checkCanPay(PayCall memory q, bytes memory expected) internal {
        (bool okPre, uint8 reason) = c.canPay(q.id, q.rid, q.payer, q.amount);
        // canPay covers the contract's own rules only; token-level failures (blacklist, bad signature) come after
        bool contractOk = expected.length == 0 || keccak256(expected) == keccak256(BL_ERR)
            || keccak256(expected) == keccak256(SIG_ERR);
        if (okPre != contractOk || (okPre && reason != 0)) {
            mismatches++;
            lastMismatch = "canPay";
        }
    }

    function pay(uint256 seed) external {
        PayCall memory q = _payArgs(seed);
        bytes memory expected = _payExpected(q.id, q.rid, q.payer, q.amount, q.price);
        _checkCanPay(q, expected);
        uint256 before = t.balanceOf(q.payer);
        vm.prank(q.payer);
        (bool ok, bytes memory ret) = address(c).call(abi.encodeCall(c.pay, (q.id, q.rid, q.amount)));
        _compare("pay", ok, ret, expected);
        if (ok && expected.length == 0) {
            if (before - t.balanceOf(q.payer) != q.amount) badTransfers++;
            _recordPay(q.id, q.rid, q.payer, q.amount);
            calls["pay_ok"]++;
        } else {
            if (t.balanceOf(q.payer) != before) badTransfers++;
            calls["pay_rej"]++;
        }
    }

    /// The same payment through a signed EIP-3009 authorization that a third party submits.
    function payWithAuth(uint256 seed) external {
        PayCall memory q = _payArgs(seed);
        if (q.payerIdx == 3) {
            q.payerIdx = 0;
            q.payer = payers[0];
        }
        bool badSig = _h(seed, "badsig") % 7 == 0; // signed for another requestId
        bytes memory data = _authData(q, badSig);
        bytes memory expected = _payExpected(q.id, q.rid, q.payer, q.amount, q.price);
        if (expected.length == 0 && badSig) {
            expected = SIG_ERR;
            calls["rej_badSignature"]++;
        }
        _checkCanPay(q, expected);
        address submitter = _h(seed, "sub") % 2 == 0 ? outsider : providers[seed % 2];
        _runAuth(q, submitter, data, expected);
    }

    function _authData(PayCall memory q, bool badSig) internal view returns (bytes memory) {
        uint256 vb = block.timestamp + 600;
        bytes32 signedRid = badSig ? keccak256(abi.encode(q.rid, "other")) : q.rid;
        bytes memory sig = _sign(payerPks[q.payerIdx], q.amount, vb, c.authorizationNonce(q.id, signedRid));
        return abi.encodeCall(c.payWithAuthorization, (q.id, q.rid, q.payer, q.amount, 0, vb, sig));
    }

    function _runAuth(PayCall memory q, address submitter, bytes memory data, bytes memory expected) internal {
        uint256 before = t.balanceOf(q.payer);
        uint256 subBefore = t.balanceOf(submitter);
        vm.prank(submitter);
        (bool ok, bytes memory ret) = address(c).call(data);
        _compare("payWithAuth", ok, ret, expected);
        if (t.balanceOf(submitter) != subBefore && submitter != q.payer) badTransfers++;
        if (ok && expected.length == 0) {
            if (before - t.balanceOf(q.payer) != q.amount) badTransfers++;
            _recordPay(q.id, q.rid, q.payer, q.amount);
            calls["auth_ok"]++;
        } else {
            if (t.balanceOf(q.payer) != before) badTransfers++;
            calls["auth_rej"]++;
        }
    }

    function refund(uint256 seed) external {
        uint256 start = _h(seed, "rid") % 33;
        bytes32 rid = pool[start];
        if (_h(seed, "paidfirst") % 4 != 0) {
            for (uint256 i = 0; i < 33; i++) {
                bytes32 r = pool[(start + i) % 33];
                if (pm[r].payer != address(0)) {
                    rid = r;
                    break;
                }
            }
        }
        P storage p = pm[rid];
        address caller;
        if (_h(seed, "caller") % 4 == 0 || p.payer == address(0)) caller = [outsider, payeeA, providers[1]][seed % 3];
        else caller = m[p.endpointId].provider;
        bytes memory expected;
        if (p.payer == address(0)) {
            expected = abi.encodeWithSelector(HskPayPerRequest.UnknownPayment.selector, rid);
        } else if (caller != m[p.endpointId].provider) {
            expected = abi.encodeWithSelector(HskPayPerRequest.NotProvider.selector);
        } else if (p.refunded) {
            expected = abi.encodeWithSelector(HskPayPerRequest.AlreadyRefunded.selector, rid);
        } else if (p.amount > mCredit[m[p.endpointId].payee]) {
            expected = abi.encodeWithSelector(
                HskPayPerRequest.InsufficientCredit.selector, p.amount, mCredit[m[p.endpointId].payee]
            );
            calls["rej_insufficientCredit"]++;
        } else if (mBlacklisted[p.payer]) {
            expected = BL_ERR;
            calls["rej_blacklisted"]++;
        }
        address payer = p.payer;
        uint256 before = payer == address(0) ? 0 : t.balanceOf(payer);
        vm.prank(caller);
        (bool ok, bytes memory ret) = address(c).call(abi.encodeCall(c.refund, (rid)));
        _compare("refund", ok, ret, expected);
        if (ok && expected.length == 0) {
            if (t.balanceOf(payer) - before != p.amount) badTransfers++;
            p.refunded = true;
            ghostRefunds[rid]++;
            E storage e = m[p.endpointId];
            e.totalRefunded += p.amount;
            e.refundCount++;
            mCredit[e.payee] -= p.amount;
            mRefunded += p.amount;
            calls["refund_ok"]++;
        } else {
            if (payer != address(0) && t.balanceOf(payer) != before) badTransfers++;
            calls["refund_rej"]++;
        }
    }

    function withdraw(uint256 seed) external {
        uint256 as_ = _h(seed, "acct") % 6;
        address account = as_ == 5 ? outsider : creditHolders[as_];
        if (_h(seed, "rich") % 3 != 0) {
            for (uint256 i = 0; i < 5; i++) {
                if (mCredit[creditHolders[i]] > mCredit[account]) account = creditHolders[i];
            }
        }
        address[5] memory tos = [account, outsider, blSink, address(0), address(c)];
        uint256 ts = _h(seed, "to") % 10;
        address to = tos[ts < 5 ? ts : (ts % 2 == 0 ? 0 : 1)];
        uint256 credit = mCredit[account];
        uint256 ak = _h(seed, "amt") % 6;
        uint256 amount = ak == 0 ? 0 : (ak == 1 ? credit + 1 : (ak == 2 ? credit / 2 : (ak == 3 ? 1 : credit)));
        bytes memory expected;
        if (to == address(0)) {
            expected = abi.encodeWithSelector(HskPayPerRequest.ZeroAddress.selector);
        } else if (amount == 0) {
            expected = abi.encodeWithSelector(HskPayPerRequest.ZeroAmount.selector);
        } else if (amount > credit) {
            expected = abi.encodeWithSelector(HskPayPerRequest.InsufficientCredit.selector, amount, credit);
            calls["rej_insufficientCredit"]++;
        } else if (mBlacklisted[to]) {
            expected = BL_ERR;
            calls["rej_blacklisted"]++;
        }
        uint256 before = t.balanceOf(to);
        vm.prank(account);
        (bool ok, bytes memory ret) = address(c).call(abi.encodeCall(c.withdraw, (to, amount)));
        _compare("withdraw", ok, ret, expected);
        if (ok && expected.length == 0) {
            if (to == address(c)) {
                if (t.balanceOf(to) != before) badTransfers++; // c -> c: balance unchanged, credit gone
                mUncredited += amount;
            } else if (t.balanceOf(to) - before != amount) {
                badTransfers++;
            }
            mCredit[account] -= amount;
            mWithdrawn += amount;
            calls["withdraw_ok"]++;
        } else {
            if (t.balanceOf(to) != before) badTransfers++;
            calls["withdraw_rej"]++;
        }
    }

    function donate(uint256 seed) external {
        uint256 amount = 1 + _h(seed, "don") % 1_000e6;
        vm.prank(outsider);
        t.transfer(address(c), amount);
        mUncredited += amount;
        calls["donate"]++;
    }

    function toggleBlacklist(uint256) external {
        address who = payers[2];
        vm.prank(blacklister);
        if (mBlacklisted[who]) usdc.unBlacklist(who);
        else usdc.blacklist(who);
        mBlacklisted[who] = !mBlacklisted[who];
        calls["blacklist_toggle"]++;
    }

    function warp(uint256 secs) external {
        secs = bound(secs, 1, 3 days);
        vm.warp(block.timestamp + secs);
        vm.roll(block.number + 1 + secs / 2);
        calls["warp"]++;
    }

    // ------------------------------------------------------------------ views for the invariants
    function modelEndpoint(uint256 id) external view returns (E memory) {
        return m[id];
    }

    function modelPayment(bytes32 rid) external view returns (P memory) {
        return pm[rid];
    }

    function sumModelCredits() external view returns (uint256 s) {
        for (uint256 i = 0; i < 5; i++) s += mCredit[creditHolders[i]];
        s += mCredit[outsider];
    }

    function holders() external view returns (address[6] memory h) {
        for (uint256 i = 0; i < 5; i++) h[i] = creditHolders[i];
        h[5] = outsider;
    }

    function summary() external view {
        for (uint256 i = 0; i < callNames.length; i++) console.log(callNames[i], calls[callNames[i]]);
    }
}

contract HskPayPerRequestInvariants is StdInvariant, FiatTokenFixture {
    HskPayPerRequest c;
    PaywallHandler h;

    function setUp() public {
        vm.warp(1_760_000_000);
        vm.roll(100);
        _loadUsdc();
        c = new HskPayPerRequest(IERC20(address(usdc)));
        h = new PaywallHandler(c, usdc, minter, blacklister);
        targetContract(address(h));
        bytes4[] memory sel = new bytes4[](11);
        sel[0] = PaywallHandler.register.selector;
        sel[1] = PaywallHandler.setPrice.selector;
        sel[2] = PaywallHandler.setActive.selector;
        sel[3] = PaywallHandler.pay.selector;
        sel[4] = PaywallHandler.payWithAuth.selector;
        sel[5] = PaywallHandler.refund.selector;
        sel[6] = PaywallHandler.withdraw.selector;
        sel[7] = PaywallHandler.warp.selector;
        sel[8] = PaywallHandler.approve.selector;
        sel[9] = PaywallHandler.donate.selector;
        sel[10] = PaywallHandler.toggleBlacklist.selector;
        targetSelector(FuzzSelector({addr: address(h), selectors: sel}));
    }

    /// I0: every call succeeded or reverted with exactly the revert data the model predicted; canPay agreed.
    function invariant_matchesModel() public view {
        assertEq(h.mismatches(), 0, h.lastMismatch());
    }

    /// I1 solvency: token balance == totalCredits + uncredited tokens; totalCredits == sum of every credit.
    function invariant_solvency() public view {
        assertEq(_bal(address(c)), c.totalCredits() + h.mUncredited(), "balance == totalCredits + uncredited");
        assertGe(_bal(address(c)), c.totalCredits(), "never below totalCredits");
        address[6] memory hs = h.holders();
        uint256 s;
        for (uint256 i = 0; i < 6; i++) {
            s += c.creditOf(hs[i]);
            assertEq(c.creditOf(hs[i]), h.mCredit(hs[i]), "credit == model");
        }
        assertEq(s, c.totalCredits(), "sum of credits == totalCredits");
        assertEq(h.sumModelCredits(), c.totalCredits(), "model credits == totalCredits");
    }

    /// I2 conservation: sum of credits + withdrawn == total paid - refunded (and every total matches the model).
    function invariant_conservation() public view {
        assertEq(c.totalCredits() + c.totalWithdrawn(), c.totalPaid() - c.totalRefunded(), "credits + withdrawn");
        assertEq(c.totalPaid(), h.mPaid());
        assertEq(c.totalRefunded(), h.mRefunded());
        assertEq(c.totalWithdrawn(), h.mWithdrawn());
    }

    /// I3: each requestId is paid at most once and refunded at most once; the stored payment matches the model.
    function invariant_requestIdsPaidAtMostOnce() public view {
        for (uint256 i = 0; i < 33; i++) {
            bytes32 rid = h.pool(i);
            assertLe(h.ghostPays(rid), 1, "paid at most once");
            assertLe(h.ghostRefunds(rid), h.ghostPays(rid), "refunded at most once, only if paid");
            assertEq(c.isPaid(rid), h.ghostPays(rid) == 1, "isPaid");
            HskPayPerRequest.Payment memory p = c.getPayment(rid);
            PaywallHandler.P memory q = h.modelPayment(rid);
            assertEq(p.payer, q.payer);
            assertEq(uint256(p.endpointId), q.endpointId);
            assertEq(p.amount, q.amount, "amount == price in force when paid");
            assertEq(p.refunded, q.refunded);
            if (p.payer != address(0)) {
                address bound = c.payerOf(rid);
                assertTrue(bound == address(0) || bound == p.payer, "bound requestIds are paid by their payer");
            }
        }
        assertEq(c.isPaid(bytes32(0)), false, "zero requestId never paid");
    }

    /// I4: per-endpoint accounting matches the model and sums to the global totals.
    function invariant_endpointAccounting() public view {
        uint256 n = c.endpointCount();
        assertEq(n, h.count());
        uint256 paid;
        uint256 refunded;
        for (uint256 id = 1; id <= n; id++) {
            HskPayPerRequest.Endpoint memory e = c.getEndpoint(id);
            PaywallHandler.E memory q = h.modelEndpoint(id);
            assertEq(e.provider, q.provider);
            assertEq(e.payee, q.payee);
            assertEq(e.price, q.price);
            assertEq(e.active, q.active);
            assertEq(e.paidCount, q.paidCount);
            assertEq(e.totalPaid, q.totalPaid);
            assertEq(e.totalRefunded, q.totalRefunded);
            assertEq(e.refundCount, q.refundCount);
            assertLe(e.totalRefunded, e.totalPaid);
            paid += e.totalPaid;
            refunded += e.totalRefunded;
        }
        assertEq(paid, c.totalPaid(), "sum of endpoint totals == totalPaid");
        address[6] memory hs = h.holders();
        for (uint256 i = 0; i < 3; i++) {
            for (uint256 j = 3; j < 5; j++) {
                bool want = hs[i] == hs[j] || h.mApproved(hs[i], hs[j]);
                assertEq(c.isProviderApproved(hs[i], hs[j]), want, "consent view == model");
            }
        }
        assertEq(refunded, c.totalRefunded(), "sum of endpoint refunds == totalRefunded");
    }

    /// I5: every token movement had exactly the predicted size (payer charged the price, refunds and withdrawals exact,
    ///     submitters never charged, nothing moved on a failed call).
    function invariant_exactTransfers() public view {
        assertEq(h.badTransfers(), 0, "balance deltas");
    }

    function afterInvariant() public view {
        h.summary();
    }
}
