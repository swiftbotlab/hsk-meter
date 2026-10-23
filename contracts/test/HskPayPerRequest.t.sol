// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {HskPayPerRequest} from "../src/HskPayPerRequest.sol";
import {FiatTokenFixture, SmartAccount, HookToken, ReentrantAccount} from "./Base.sol";

/// @dev Unit + fuzz tests for HskPayPerRequest against the real FiatTokenV2_2 (USDC.e) code. Amounts are USDC.e base
///      units (6 decimals): PRICE = 0.01 USDC.e.
contract HskPayPerRequestTest is FiatTokenFixture {
    HskPayPerRequest c;
    address provider = makeAddr("provider");
    address payee = makeAddr("payee");
    uint256 agentPk = 0xA11CE;
    uint256 agent2Pk = 0xB0B;
    address agent;
    address agent2;
    address outsider = makeAddr("outsider");
    address relayer = makeAddr("relayer");
    uint128 constant PRICE = 10_000;
    uint256 constant FUNDS = 1_000e6;

    event EndpointRegistered(
        uint256 indexed id, address indexed provider, address indexed payee, uint256 price, string name
    );
    event PriceChanged(uint256 indexed id, uint256 oldPrice, uint256 newPrice);
    event ActiveSet(uint256 indexed id, bool active);
    event Paid(
        uint256 indexed endpointId, bytes32 indexed requestId, address indexed payer, address payee, uint256 amount
    );
    event PaidWithAuthorization(bytes32 indexed requestId, bytes32 indexed nonce, address indexed submitter);
    event Refunded(uint256 indexed endpointId, bytes32 indexed requestId, address indexed payer, uint256 amount);
    event Withdrawn(address indexed account, address indexed to, uint256 amount);
    event ProviderApproval(address indexed payee, address indexed provider, bool approved);

    function setUp() public {
        vm.warp(1_760_000_000);
        vm.roll(1_000);
        _loadUsdc();
        agent = vm.addr(agentPk);
        agent2 = vm.addr(agent2Pk);
        c = new HskPayPerRequest(IERC20(address(usdc)));
        address[4] memory payers = [agent, agent2, outsider, provider];
        for (uint256 i = 0; i < 4; i++) {
            _mint(payers[i], FUNDS);
            vm.prank(payers[i]);
            IERC20(address(usdc)).approve(address(c), type(uint256).max);
        }
        vm.prank(payee); // the payee consents to endpoints registered by `provider`
        c.approveProvider(provider, true);
    }

    // ------------------------------------------------------------------ helpers
    function _register(uint128 price) internal returns (uint256 id) {
        vm.prank(provider);
        id = c.registerEndpoint(price, payee, "GET /v1/weather");
    }

    function _rid(address payer, string memory tag) internal view returns (bytes32) {
        return c.requestIdFor(payer, keccak256(bytes(tag)));
    }

    function _pay(address payer, uint256 id, bytes32 rid, uint256 amount) internal {
        vm.prank(payer);
        c.pay(id, rid, amount);
    }

    function _authSig(uint256 pk, uint256 id, bytes32 rid, uint256 value, uint256 validBefore)
        internal
        view
        returns (bytes memory)
    {
        return _signReceive(pk, address(c), value, 0, validBefore, c.authorizationNonce(id, rid));
    }

    function _accounting() internal view {
        assertEq(_bal(address(c)), c.totalCredits(), "balance == totalCredits");
        assertEq(c.totalCredits() + c.totalWithdrawn(), c.totalPaid() - c.totalRefunded(), "conservation");
    }

    // ------------------------------------------------------------------ constructor
    function test_constructor_rejectsZeroAndNonContract() public {
        vm.expectRevert(HskPayPerRequest.ZeroAddress.selector);
        new HskPayPerRequest(IERC20(address(0)));
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.NotAContract.selector, outsider));
        new HskPayPerRequest(IERC20(outsider));
        assertEq(address(c.token()), USDC_ADDR);
        assertEq(usdc.decimals(), 6);
        assertEq(usdc.name(), "Bridged USDC");
        assertEq(usdc.version(), "2");
    }

    // ------------------------------------------------------------------ registerEndpoint
    function test_register_storesEndpoint() public {
        vm.expectEmit(true, true, true, true, address(c));
        emit EndpointRegistered(1, provider, payee, PRICE, "GET /v1/weather");
        uint256 id = _register(PRICE);
        assertEq(id, 1);
        assertEq(c.endpointCount(), 1);
        HskPayPerRequest.Endpoint memory e = c.getEndpoint(id);
        assertEq(e.provider, provider);
        assertTrue(e.active);
        assertEq(e.createdAt, block.timestamp);
        assertEq(e.payee, payee);
        assertEq(e.paidCount, 0);
        assertEq(e.price, PRICE);
        assertEq(e.totalPaid, 0);
        assertEq(e.totalRefunded, 0);
        assertEq(e.refundCount, 0);
        assertEq(c.nameOf(id), "GET /v1/weather");
    }

    function test_register_idsIncrementAndIndexByProvider() public {
        uint256 a = _register(PRICE);
        vm.prank(outsider);
        uint256 b = c.registerEndpoint(1, outsider, "other provider");
        uint256 d = _register(2 * PRICE);
        assertEq(a, 1);
        assertEq(b, 2);
        assertEq(d, 3);
        uint256[] memory mine = c.endpointsOf(provider);
        assertEq(mine.length, 2);
        assertEq(mine[0], 1);
        assertEq(mine[1], 3);
        assertEq(c.endpointsOf(outsider).length, 1);
        assertEq(c.endpointsOf(agent).length, 0);
    }

    function test_register_rejectsBadInput() public {
        vm.startPrank(provider);
        vm.expectRevert(HskPayPerRequest.ZeroAddress.selector);
        c.registerEndpoint(PRICE, address(0), "x");
        vm.expectRevert(HskPayPerRequest.InvalidPayee.selector);
        c.registerEndpoint(PRICE, address(c), "x");
        vm.expectRevert(HskPayPerRequest.InvalidPrice.selector);
        c.registerEndpoint(0, payee, "x");
        vm.expectRevert(HskPayPerRequest.InvalidName.selector);
        c.registerEndpoint(PRICE, payee, "");
        string memory n65 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        assertEq(bytes(n65).length, 65);
        vm.expectRevert(HskPayPerRequest.InvalidName.selector);
        c.registerEndpoint(PRICE, payee, n65);
        string memory n64 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        uint256 id = c.registerEndpoint(PRICE, payee, n64); // exactly 64 bytes is fine
        vm.stopPrank();
        assertEq(c.nameOf(id), n64);
        assertEq(c.endpointCount(), 1);
    }

    function test_register_checkOrder() public {
        vm.startPrank(provider);
        vm.expectRevert(HskPayPerRequest.ZeroAddress.selector); // payee first
        c.registerEndpoint(0, address(0), "");
        vm.expectRevert(HskPayPerRequest.InvalidPayee.selector);
        c.registerEndpoint(0, address(c), "");
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.PayeeNotApproved.selector, agent)); // then consent
        c.registerEndpoint(0, agent, "");
        vm.expectRevert(HskPayPerRequest.InvalidPrice.selector); // then price
        c.registerEndpoint(0, payee, "");
        vm.stopPrank();
    }

    function test_register_payeeMustConsent() public {
        // nobody can attach a stranger's address as payee (and later refund out of the stranger's credit)
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.PayeeNotApproved.selector, agent2));
        vm.prank(outsider);
        c.registerEndpoint(PRICE, agent2, "griefing attempt");
        assertFalse(c.isProviderApproved(agent2, outsider));
        assertTrue(c.isProviderApproved(outsider, outsider), "a provider may always pay itself");
        vm.expectEmit(true, true, false, true, address(c));
        emit ProviderApproval(agent2, outsider, true);
        vm.prank(agent2);
        c.approveProvider(outsider, true);
        assertTrue(c.isProviderApproved(agent2, outsider));
        vm.prank(outsider);
        uint256 id = c.registerEndpoint(PRICE, agent2, "approved");
        // revoking stops NEW endpoints only; the existing one keeps paying agent2
        vm.prank(agent2);
        c.approveProvider(outsider, false);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.PayeeNotApproved.selector, agent2));
        vm.prank(outsider);
        c.registerEndpoint(PRICE, agent2, "after revoke");
        _pay(agent, id, _rid(agent, "x"), PRICE);
        assertEq(c.creditOf(agent2), PRICE);
        vm.expectRevert(HskPayPerRequest.ZeroAddress.selector);
        vm.prank(agent2);
        c.approveProvider(address(0), true);
    }

    function test_register_maxPrice() public {
        vm.prank(provider);
        uint256 id = c.registerEndpoint(type(uint128).max, payee, "expensive");
        assertEq(c.getEndpoint(id).price, type(uint128).max);
    }

    // ------------------------------------------------------------------ setPrice / setActive
    function test_setPrice_onlyProviderKnownNonZero() public {
        uint256 id = _register(PRICE);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, 0));
        vm.prank(provider);
        c.setPrice(0, PRICE);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, 2));
        vm.prank(provider);
        c.setPrice(2, PRICE);
        vm.expectRevert(HskPayPerRequest.NotProvider.selector);
        vm.prank(payee); // the payee is not the provider
        c.setPrice(id, PRICE);
        vm.expectRevert(HskPayPerRequest.InvalidPrice.selector);
        vm.prank(provider);
        c.setPrice(id, 0);
        vm.expectEmit(true, false, false, true, address(c));
        emit PriceChanged(id, PRICE, 3 * PRICE);
        vm.prank(provider);
        c.setPrice(id, 3 * PRICE);
        assertEq(c.getEndpoint(id).price, 3 * PRICE);
        vm.expectEmit(true, false, false, true, address(c)); // same value: allowed, still emits
        emit PriceChanged(id, 3 * PRICE, 3 * PRICE);
        vm.prank(provider);
        c.setPrice(id, 3 * PRICE);
    }

    function test_setPrice_appliesToNewPaymentsOnly() public {
        uint256 id = _register(PRICE);
        bytes32 r1 = _rid(agent, "r1");
        bytes32 r2 = _rid(agent, "r2");
        _pay(agent, id, r1, PRICE);
        vm.prank(provider);
        c.setPrice(id, 2 * PRICE);
        // a client holding the old quote is refused, never charged a different amount
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.WrongAmount.selector, PRICE, 2 * PRICE));
        _pay(agent, id, r2, PRICE);
        _pay(agent, id, r2, 2 * PRICE);
        assertEq(c.getPayment(r1).amount, PRICE, "old payment keeps its amount");
        assertEq(c.getPayment(r2).amount, 2 * PRICE);
        assertEq(c.creditOf(payee), 3 * PRICE);
        // a refund returns what was paid, not the current price
        uint256 before = _bal(agent);
        vm.prank(provider);
        c.refund(r1);
        assertEq(_bal(agent) - before, PRICE);
        _accounting();
    }

    function test_setActive_pausesNewPaymentsOnly() public {
        uint256 id = _register(PRICE);
        bytes32 r1 = _rid(agent, "r1");
        bytes32 r2 = _rid(agent, "r2");
        _pay(agent, id, r1, PRICE);
        _pay(agent, id, r2, PRICE);
        vm.expectRevert(HskPayPerRequest.NotProvider.selector);
        vm.prank(outsider);
        c.setActive(id, false);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, 9));
        vm.prank(provider);
        c.setActive(9, false);
        vm.expectEmit(true, false, false, true, address(c));
        emit ActiveSet(id, false);
        vm.prank(provider);
        c.setActive(id, false);
        assertFalse(c.getEndpoint(id).active);
        bytes32 r3 = _rid(agent, "r3");
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.EndpointInactive.selector, id));
        _pay(agent, id, r3, PRICE);
        // refunds and withdrawals still work while inactive
        vm.prank(provider);
        c.refund(r1);
        vm.prank(payee);
        c.withdraw(payee, PRICE);
        assertEq(c.creditOf(payee), 0);
        vm.prank(provider);
        c.setActive(id, true);
        _pay(agent, id, r3, PRICE);
        assertEq(c.getEndpoint(id).paidCount, 3);
        _accounting();
    }

    // ------------------------------------------------------------------ pay (approve flow)
    function test_pay_creditsPayeeAndRecords() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "GET /v1/weather?city=Paris");
        vm.roll(1_234);
        vm.warp(1_760_000_100);
        uint256 agentBefore = _bal(agent);
        vm.expectEmit(true, true, true, true, address(c));
        emit Paid(id, rid, agent, payee, PRICE);
        _pay(agent, id, rid, PRICE);
        HskPayPerRequest.Payment memory p = c.getPayment(rid);
        assertEq(p.payer, agent);
        assertEq(p.endpointId, id);
        assertFalse(p.refunded);
        assertEq(p.amount, PRICE);
        assertEq(p.paidAt, 1_760_000_100);
        assertEq(p.paidBlock, 1_234);
        assertTrue(c.isPaid(rid));
        assertEq(c.creditOf(payee), PRICE);
        assertEq(c.totalPaid(), PRICE);
        assertEq(c.totalCredits(), PRICE);
        assertEq(_bal(address(c)), PRICE);
        assertEq(agentBefore - _bal(agent), PRICE, "payer charged exactly the price");
        assertEq(_bal(payee), 0, "pull model: nothing is pushed to the payee");
        HskPayPerRequest.Endpoint memory e = c.getEndpoint(id);
        assertEq(e.paidCount, 1);
        assertEq(e.totalPaid, PRICE);
        _accounting();
    }

    function test_pay_needsAllowanceAndBalance() public {
        uint256 id = _register(PRICE);
        address poor = makeAddr("poor");
        bytes32 r1 = _rid(poor, "1");
        _mint(poor, PRICE);
        vm.expectRevert(bytes("ERC20: transfer amount exceeds allowance"));
        _pay(poor, id, r1, PRICE);
        vm.prank(poor);
        IERC20(address(usdc)).approve(address(c), PRICE - 1);
        vm.expectRevert(bytes("ERC20: transfer amount exceeds allowance"));
        _pay(poor, id, r1, PRICE);
        vm.prank(poor);
        IERC20(address(usdc)).approve(address(c), 10 * PRICE);
        _pay(poor, id, r1, PRICE);
        bytes32 r2 = _rid(poor, "2");
        vm.expectRevert(bytes("ERC20: transfer amount exceeds balance"));
        _pay(poor, id, r2, PRICE);
        assertFalse(c.isPaid(r2), "a failed payment records nothing");
        assertEq(c.totalPaid(), PRICE);
        _accounting();
    }

    function test_pay_unknownEndpoint() public {
        _register(PRICE);
        bytes32 rid = _rid(agent, "x"); // computed first: _rid is an external call and would eat expectRevert
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, 0));
        _pay(agent, 0, rid, PRICE);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, 2));
        _pay(agent, 2, rid, PRICE);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, type(uint256).max));
        _pay(agent, type(uint256).max, rid, PRICE);
    }

    function test_pay_zeroRequestId() public {
        uint256 id = _register(PRICE);
        vm.expectRevert(HskPayPerRequest.ZeroRequestId.selector);
        _pay(agent, id, bytes32(0), PRICE);
    }

    function test_pay_boundToOtherPayer() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "mine");
        assertEq(c.payerOf(rid), agent);
        // a front-runner copying the agent's requestId from the mempool cannot pay (and so cannot burn) it
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.RequestNotForPayer.selector, agent));
        _pay(outsider, id, rid, PRICE);
        _pay(agent, id, rid, PRICE);
        assertEq(c.getPayment(rid).payer, agent);
    }

    function test_pay_unboundAnyoneCanPayOnce() public {
        uint256 id = _register(PRICE);
        bytes32 rid = c.requestIdFor(address(0), keccak256("open"));
        assertEq(c.payerOf(rid), address(0));
        assertTrue(rid != bytes32(0));
        _pay(outsider, id, rid, PRICE);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.AlreadyPaid.selector, rid));
        _pay(agent, id, rid, PRICE);
        assertEq(c.getPayment(rid).payer, outsider);
    }

    function test_pay_alreadyPaidAcrossEndpoints() public {
        uint256 a = _register(PRICE);
        uint256 b = _register(PRICE);
        bytes32 rid = _rid(agent, "once");
        _pay(agent, a, rid, PRICE);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.AlreadyPaid.selector, rid));
        _pay(agent, a, rid, PRICE); // a retried payment never charges twice
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.AlreadyPaid.selector, rid));
        _pay(agent, b, rid, PRICE); // requestIds are global, not per endpoint
        assertEq(c.totalPaid(), PRICE);
        assertEq(c.getEndpoint(b).paidCount, 0);
    }

    function test_pay_wrongAmount() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "amount");
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.WrongAmount.selector, PRICE - 1, PRICE));
        _pay(agent, id, rid, PRICE - 1);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.WrongAmount.selector, PRICE + 1, PRICE));
        _pay(agent, id, rid, PRICE + 1);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.WrongAmount.selector, 0, PRICE));
        _pay(agent, id, rid, 0);
        assertFalse(c.isPaid(rid), "a failed payment does not use up the requestId");
        _pay(agent, id, rid, PRICE);
    }

    function test_pay_checkOrder() public {
        uint256 id = _register(PRICE);
        bytes32 paidRid = _rid(agent, "paid");
        _pay(agent, id, paidRid, PRICE);
        bytes32 foreign = _rid(agent2, "foreign");
        // unknown endpoint beats everything
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, 7));
        _pay(agent, 7, bytes32(0), 0);
        vm.prank(provider);
        c.setActive(id, false);
        // inactive beats zero id / binding / already paid / amount
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.EndpointInactive.selector, id));
        _pay(agent, id, bytes32(0), 0);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.EndpointInactive.selector, id));
        _pay(agent, id, paidRid, PRICE);
        vm.prank(provider);
        c.setActive(id, true);
        vm.expectRevert(HskPayPerRequest.ZeroRequestId.selector);
        _pay(agent, id, bytes32(0), 0);
        // binding beats already-paid and amount
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.RequestNotForPayer.selector, agent));
        _pay(agent2, id, paidRid, 0);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.RequestNotForPayer.selector, agent2));
        _pay(agent, id, foreign, 0);
        // already-paid beats amount (a retry with a stale price still says "already paid")
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.AlreadyPaid.selector, paidRid));
        _pay(agent, id, paidRid, 0);
    }

    function test_pay_refundedRequestStaysUsed() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "refund-me");
        _pay(agent, id, rid, PRICE);
        vm.prank(provider);
        c.refund(rid);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.AlreadyPaid.selector, rid));
        _pay(agent, id, rid, PRICE);
        assertTrue(c.isPaid(rid));
        assertTrue(c.getPayment(rid).refunded);
    }

    function test_pay_selfPayAndMultiEndpointCreditsAggregate() public {
        uint256 a = _register(PRICE);
        vm.prank(payee);
        c.approveProvider(outsider, true);
        vm.prank(outsider);
        uint256 b = c.registerEndpoint(3 * PRICE, payee, "same payee, other provider");
        _pay(agent, a, _rid(agent, "1"), PRICE);
        _pay(agent2, b, _rid(agent2, "2"), 3 * PRICE);
        _mint(payee, PRICE);
        vm.prank(payee);
        IERC20(address(usdc)).approve(address(c), PRICE);
        _pay(payee, a, _rid(payee, "self"), PRICE); // the payee may pay its own endpoint
        assertEq(c.creditOf(payee), 5 * PRICE);
        _accounting();
    }

    function test_pay_fromSmartAccount() public {
        uint256 id = _register(PRICE);
        SmartAccount acct = new SmartAccount(agent);
        _mint(address(acct), 1e6);
        bytes32 rid = c.requestIdFor(address(acct), keccak256("sa"));
        vm.startPrank(agent);
        acct.exec(address(usdc), abi.encodeCall(IERC20.approve, (address(c), PRICE)));
        acct.exec(address(c), abi.encodeCall(HskPayPerRequest.pay, (id, rid, PRICE)));
        vm.stopPrank();
        assertEq(c.getPayment(rid).payer, address(acct));
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.RequestNotForPayer.selector, address(acct)));
        _pay(outsider, id, rid, PRICE);
    }

    function test_noNativeCoinAccepted() public {
        vm.deal(agent, 1 ether);
        vm.prank(agent);
        (bool ok,) = address(c).call{value: 1}("");
        assertFalse(ok, "plain transfer must fail");
        vm.prank(agent);
        (ok,) = address(c).call{value: 1}(hex"deadbeef");
        assertFalse(ok, "unknown selector must fail");
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "v");
        vm.prank(agent);
        (ok,) = address(c).call{value: 1}(abi.encodeCall(HskPayPerRequest.pay, (id, rid, PRICE)));
        assertFalse(ok, "pay is not payable");
        assertEq(address(c).balance, 0);
    }

    function test_tokenPaused_nothingRecorded() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "paused");
        vm.prank(pauser);
        usdc.pause();
        vm.expectRevert(bytes("Pausable: paused"));
        _pay(agent, id, rid, PRICE);
        assertFalse(c.isPaid(rid), "the token reverting rolls the record back");
        assertEq(c.totalPaid(), 0);
        vm.prank(pauser);
        usdc.unpause();
        _pay(agent, id, rid, PRICE);
        _accounting();
    }

    function test_donationIsNotCreditedAndAccountingHolds() public {
        uint256 id = _register(PRICE);
        _pay(agent, id, _rid(agent, "1"), PRICE);
        vm.prank(outsider);
        IERC20(address(usdc)).transfer(address(c), 5 * PRICE); // tokens sent here directly
        assertEq(_bal(address(c)), 6 * PRICE);
        assertEq(c.totalCredits(), PRICE, "a direct transfer credits nobody");
        assertEq(c.creditOf(outsider), 0);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.InsufficientCredit.selector, 1, 0));
        vm.prank(outsider);
        c.withdraw(outsider, 1);
        vm.prank(payee);
        c.withdraw(payee, PRICE);
        assertEq(_bal(address(c)), 5 * PRICE);
        assertEq(c.totalCredits() + c.totalWithdrawn(), c.totalPaid() - c.totalRefunded(), "conservation");
    }

    // ------------------------------------------------------------------ payWithAuthorization (EIP-3009)
    function test_auth_happyPath_payerNeedsNoGas() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "auth-1");
        bytes memory sig = _authSig(agentPk, id, rid, PRICE, block.timestamp + 300);
        bytes32 nonce = c.authorizationNonce(id, rid);
        assertEq(nonce, keccak256(abi.encode(address(c), id, rid)));
        uint256 agentBefore = _bal(agent);
        assertEq(agent.balance, 0, "the payer holds no native coin");
        vm.expectEmit(true, true, true, true, address(c));
        emit Paid(id, rid, agent, payee, PRICE);
        vm.expectEmit(true, true, true, true, address(c));
        emit PaidWithAuthorization(rid, nonce, relayer);
        vm.prank(relayer); // the seller's server / a facilitator submits and pays the gas
        c.payWithAuthorization(id, rid, agent, PRICE, 0, block.timestamp + 300, sig);
        assertEq(agentBefore - _bal(agent), PRICE);
        assertEq(_bal(relayer), 0, "the submitter gets nothing");
        HskPayPerRequest.Payment memory p = c.getPayment(rid);
        assertEq(p.payer, agent);
        assertEq(p.amount, PRICE);
        assertTrue(usdc.authorizationState(agent, nonce), "nonce used at the token");
        assertEq(c.creditOf(payee), PRICE);
        _accounting();
    }

    function test_auth_cannotBeRedirected() public {
        uint256 a = _register(PRICE);
        vm.prank(outsider);
        uint256 b = c.registerEndpoint(PRICE, outsider, "attacker endpoint, same price");
        bytes32 rid = _rid(agent, "auth-redirect");
        bytes32 rid2 = _rid(agent, "auth-other");
        uint256 vb = block.timestamp + 300;
        bytes memory sig = _authSig(agentPk, a, rid, PRICE, vb);
        // the same signature cannot pay another endpoint at the same price, nor another requestId
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        vm.prank(outsider);
        c.payWithAuthorization(b, rid, agent, PRICE, 0, vb, sig);
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        vm.prank(outsider);
        c.payWithAuthorization(a, rid2, agent, PRICE, 0, vb, sig);
        // nor change the validity window
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        c.payWithAuthorization(a, rid, agent, PRICE, 0, vb + 1, sig);
        assertFalse(c.isPaid(rid));
        assertEq(c.creditOf(outsider), 0);
        c.payWithAuthorization(a, rid, agent, PRICE, 0, vb, sig);
        assertEq(c.getPayment(rid).endpointId, a);
        _accounting();
    }

    function test_auth_cannotBeFrontRunAtTheToken() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "auth-frontrun");
        uint256 vb = block.timestamp + 300;
        bytes32 nonce = c.authorizationNonce(id, rid);
        bytes memory sig = _authSig(agentPk, id, rid, PRICE, vb);
        // someone copying the signature from the mempool cannot execute it as a bare transfer into the contract
        vm.expectRevert(bytes("FiatTokenV2: caller must be the payee"));
        vm.prank(outsider);
        usdc.receiveWithAuthorization(agent, address(c), PRICE, 0, vb, nonce, sig);
        assertFalse(usdc.authorizationState(agent, nonce));
        vm.prank(outsider);
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, sig);
        assertEq(c.getPayment(rid).payer, agent, "copying the tx only pays what the payer signed");
    }

    function test_auth_validityWindow() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "auth-window");
        bytes32 nonce = c.authorizationNonce(id, rid);
        uint256 vb = block.timestamp + 60;
        bytes memory sig = _authSig(agentPk, id, rid, PRICE, vb);
        vm.warp(vb); // validBefore is exclusive
        vm.expectRevert(bytes("FiatTokenV2: authorization is expired"));
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, sig);
        uint256 va = block.timestamp + 100;
        bytes memory early = _signReceive(agentPk, address(c), PRICE, va, va + 100, nonce);
        vm.expectRevert(bytes("FiatTokenV2: authorization is not yet valid"));
        c.payWithAuthorization(id, rid, agent, PRICE, va, va + 100, early);
        vm.warp(va + 1);
        c.payWithAuthorization(id, rid, agent, PRICE, va, va + 100, early);
        assertTrue(c.isPaid(rid));
    }

    function test_auth_replayAndCancel() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "auth-replay");
        uint256 vb = block.timestamp + 300;
        bytes memory sig = _authSig(agentPk, id, rid, PRICE, vb);
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, sig);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.AlreadyPaid.selector, rid));
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, sig);
        // a payer can cancel an unused authorization at the token; it then never pays
        bytes32 rid2 = _rid(agent, "auth-cancel");
        bytes32 nonce2 = c.authorizationNonce(id, rid2);
        bytes memory sig2 = _authSig(agentPk, id, rid2, PRICE, vb);
        bytes32 structHash = keccak256(
            abi.encode(keccak256("CancelAuthorization(address authorizer,bytes32 nonce)"), agent, nonce2)
        );
        (uint8 v, bytes32 r, bytes32 s) =
            vm.sign(agentPk, keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash)));
        usdc.cancelAuthorization(agent, nonce2, abi.encodePacked(r, s, v));
        vm.expectRevert(bytes("FiatTokenV2: authorization is used or canceled"));
        c.payWithAuthorization(id, rid2, agent, PRICE, 0, vb, sig2);
        assertFalse(c.isPaid(rid2));
        assertEq(c.totalPaid(), PRICE);
        _accounting();
    }

    function test_auth_onlyReceiveTypeAndRightSignerAccepted() public {
        uint256 id = _register(PRICE);
        bytes32 rid = c.requestIdFor(address(0), keccak256("auth-unbound"));
        uint256 vb = block.timestamp + 300;
        bytes32 nonce = c.authorizationNonce(id, rid);
        // a TransferWithAuthorization signature over the same fields is a different EIP-712 type: rejected
        bytes memory tsig = _signTransfer(agentPk, address(c), PRICE, 0, vb, nonce);
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, tsig);
        // agent2's signature presented as agent's: rejected (an unbound requestId does not relax the signer check)
        bytes memory other = _authSig(agent2Pk, id, rid, PRICE, vb);
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, other);
        vm.expectRevert(bytes("ECRecover: invalid signature length"));
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, hex"00");
        c.payWithAuthorization(id, rid, agent2, PRICE, 0, vb, other); // the real signer pays its own authorization
        assertEq(c.getPayment(rid).payer, agent2);
    }

    function test_auth_contractRulesComeFirst() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "auth-rules");
        uint256 vb = block.timestamp + 300;
        bytes memory sig = _authSig(agentPk, id, rid, PRICE, vb);
        // the bound payer must be the authorizer
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.RequestNotForPayer.selector, agent));
        c.payWithAuthorization(id, rid, agent2, PRICE, 0, vb, sig);
        // the signed value must be the price in force
        vm.prank(provider);
        c.setPrice(id, 2 * PRICE);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.WrongAmount.selector, PRICE, 2 * PRICE));
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, sig);
        vm.prank(provider);
        c.setActive(id, false);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.EndpointInactive.selector, id));
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, sig);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.UnknownEndpoint.selector, id + 1));
        c.payWithAuthorization(id + 1, rid, agent, PRICE, 0, vb, sig);
        vm.prank(provider);
        c.setActive(id, true);
        vm.prank(provider);
        c.setPrice(id, PRICE);
        c.payWithAuthorization(id, rid, agent, PRICE, 0, vb, sig);
        assertTrue(c.isPaid(rid));
    }

    function test_auth_insufficientBalanceRecordsNothing() public {
        uint256 pk = 0xD00D;
        address broke = vm.addr(pk);
        _mint(broke, PRICE - 1);
        uint256 id = _register(PRICE);
        bytes32 rid = c.requestIdFor(broke, keccak256("broke"));
        uint256 vb = block.timestamp + 300;
        bytes memory sig = _authSig(pk, id, rid, PRICE, vb);
        vm.expectRevert(bytes("ERC20: transfer amount exceeds balance"));
        c.payWithAuthorization(id, rid, broke, PRICE, 0, vb, sig);
        assertFalse(c.isPaid(rid));
        assertEq(c.totalCredits(), 0);
    }

    function test_auth_smartAccountPayerViaErc1271() public {
        uint256 id = _register(PRICE);
        SmartAccount acct = new SmartAccount(agent); // owned by agent's key
        _mint(address(acct), 1e6);
        bytes32 rid = c.requestIdFor(address(acct), keccak256("sa-auth"));
        uint256 vb = block.timestamp + 300;
        bytes32 d = _receiveDigest(address(acct), address(c), PRICE, 0, vb, c.authorizationNonce(id, rid));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(agentPk, d);
        vm.prank(relayer);
        c.payWithAuthorization(id, rid, address(acct), PRICE, 0, vb, abi.encodePacked(r, s, v));
        assertEq(c.getPayment(rid).payer, address(acct));
        assertEq(_bal(address(acct)), 1e6 - PRICE);
        // and the refund goes back to the smart account
        vm.prank(provider);
        c.refund(rid);
        assertEq(_bal(address(acct)), 1e6);
        _accounting();
    }

    function test_auth_refundAndWithdrawAfterAuthPayments() public {
        uint256 id = _register(PRICE);
        uint256 vb = block.timestamp + 300;
        bytes32 r1 = _rid(agent, "a1");
        bytes32 r2 = _rid(agent, "a2");
        c.payWithAuthorization(id, r1, agent, PRICE, 0, vb, _authSig(agentPk, id, r1, PRICE, vb));
        c.payWithAuthorization(id, r2, agent, PRICE, 0, vb, _authSig(agentPk, id, r2, PRICE, vb));
        uint256 before = _bal(agent);
        vm.prank(provider);
        c.refund(r2);
        assertEq(_bal(agent) - before, PRICE);
        vm.prank(payee);
        c.withdraw(payee, PRICE);
        assertEq(_bal(payee), PRICE);
        assertEq(c.totalCredits(), 0);
        _accounting();
    }

    // ------------------------------------------------------------------ canPay
    function test_canPay_reportsEachReason() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "ok");
        _expectReason(id, rid, agent, PRICE, 0);
        _expectReason(0, rid, agent, PRICE, 1);
        _expectReason(id, bytes32(0), agent, PRICE, 3);
        _expectReason(id, rid, agent2, PRICE, 4);
        _expectReason(id, rid, agent, PRICE + 1, 6);
        _pay(agent, id, rid, PRICE);
        _expectReason(id, rid, agent, PRICE, 5);
        vm.prank(provider);
        c.setActive(id, false);
        _expectReason(id, _rid(agent, "new"), agent, PRICE, 2);
    }

    function _expectReason(uint256 id, bytes32 rid, address payer, uint256 amount, uint8 want) internal view {
        (bool ok, uint8 reason) = c.canPay(id, rid, payer, amount);
        assertEq(reason, want, "reason");
        assertEq(ok, want == 0, "ok flag");
    }

    // ------------------------------------------------------------------ refund
    function test_refund_happyPath() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "upstream failed");
        _pay(agent, id, rid, PRICE);
        _pay(agent, id, _rid(agent, "kept"), PRICE);
        uint256 before = _bal(agent);
        vm.expectEmit(true, true, true, true, address(c));
        emit Refunded(id, rid, agent, PRICE);
        vm.prank(provider);
        c.refund(rid);
        assertEq(_bal(agent) - before, PRICE, "payer gets exactly the amount back");
        assertTrue(c.getPayment(rid).refunded);
        assertEq(c.creditOf(payee), PRICE);
        assertEq(c.totalRefunded(), PRICE);
        HskPayPerRequest.Endpoint memory e = c.getEndpoint(id);
        assertEq(e.totalRefunded, PRICE);
        assertEq(e.refundCount, 1);
        assertEq(e.paidCount, 2);
        assertEq(e.totalPaid, 2 * PRICE);
        _accounting();
    }

    function test_refund_onlyProvider() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "r");
        _pay(agent, id, rid, PRICE);
        address[3] memory notAllowed = [payee, agent, outsider];
        for (uint256 i = 0; i < 3; i++) {
            vm.expectRevert(HskPayPerRequest.NotProvider.selector);
            vm.prank(notAllowed[i]);
            c.refund(rid);
        }
    }

    function test_refund_unknownAndTwice() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "r");
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.UnknownPayment.selector, rid));
        vm.prank(provider);
        c.refund(rid);
        _pay(agent, id, rid, PRICE);
        vm.prank(provider);
        c.refund(rid);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.AlreadyRefunded.selector, rid));
        vm.prank(provider);
        c.refund(rid);
        // order: unknown payment, then provider, then already refunded
        vm.expectRevert(HskPayPerRequest.NotProvider.selector);
        vm.prank(outsider);
        c.refund(rid);
    }

    function test_refund_comesOutOfUnwithdrawnCredit() public {
        uint256 id = _register(PRICE);
        bytes32 r1 = _rid(agent, "r1");
        bytes32 r2 = _rid(agent, "r2");
        _pay(agent, id, r1, PRICE);
        _pay(agent, id, r2, PRICE);
        vm.prank(payee);
        c.withdraw(payee, 2 * PRICE - 1);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.InsufficientCredit.selector, PRICE, 1));
        vm.prank(provider);
        c.refund(r1);
        assertFalse(c.getPayment(r1).refunded);
        _accounting();
    }

    function test_refund_blacklistedPayerChangesNothing() public {
        uint256 id = _register(PRICE);
        bytes32 rid = _rid(agent, "x");
        _pay(agent, id, rid, PRICE);
        vm.prank(blacklister);
        usdc.blacklist(agent);
        vm.expectRevert(bytes("Blacklistable: account is blacklisted"));
        vm.prank(provider);
        c.refund(rid);
        assertFalse(c.getPayment(rid).refunded);
        assertEq(c.creditOf(payee), PRICE);
        assertEq(c.totalRefunded(), 0);
        _accounting();
        vm.prank(blacklister);
        usdc.unBlacklist(agent);
        vm.prank(provider);
        c.refund(rid); // the provider can retry once the payer can receive again
        assertTrue(c.getPayment(rid).refunded);
        _accounting();
    }

    /// POSITIVE CONTROL: with a hook token the payer re-enters during its refund; the guard stops it.
    function test_refund_reentrancyIsBlocked() public {
        HookToken ht = new HookToken();
        HskPayPerRequest hc = new HskPayPerRequest(IERC20(address(ht)));
        vm.prank(provider);
        uint256 id = hc.registerEndpoint(PRICE, provider, "hook endpoint");
        ReentrantAccount acct = new ReentrantAccount(hc, ht);
        ht.mint(address(acct), 10 * PRICE);
        bytes32 rid = hc.requestIdFor(address(acct), keccak256("re"));
        acct.pay(id, rid, PRICE);
        // bubble: the inner refund hits the guard, so the whole refund reverts
        acct.setMode(1);
        vm.expectRevert(abi.encodeWithSignature("ReentrancyGuardReentrantCall()"));
        vm.prank(provider);
        hc.refund(rid);
        // re-entering pay() while being refunded: same
        acct.setMode(5);
        vm.expectRevert(abi.encodeWithSignature("ReentrancyGuardReentrantCall()"));
        vm.prank(provider);
        hc.refund(rid);
        // swallow: the refund goes through exactly once
        acct.setMode(2);
        vm.prank(provider);
        hc.refund(rid);
        assertTrue(acct.innerFailed());
        assertEq(acct.received(), PRICE);
        assertEq(hc.totalRefunded(), PRICE);
        assertEq(ht.balanceOf(address(hc)), hc.totalCredits());
    }

    // ------------------------------------------------------------------ withdraw
    function test_withdraw_partialAndFull() public {
        uint256 id = _register(PRICE);
        _pay(agent, id, _rid(agent, "1"), PRICE);
        _pay(agent2, id, _rid(agent2, "2"), PRICE);
        vm.expectEmit(true, true, false, true, address(c));
        emit Withdrawn(payee, payee, PRICE / 2);
        vm.prank(payee);
        c.withdraw(payee, PRICE / 2);
        assertEq(_bal(payee), PRICE / 2);
        assertEq(c.creditOf(payee), 2 * PRICE - PRICE / 2);
        uint256 before = _bal(outsider);
        vm.prank(payee);
        c.withdraw(outsider, 2 * PRICE - PRICE / 2); // to any address
        assertEq(_bal(outsider) - before, 2 * PRICE - PRICE / 2);
        assertEq(c.creditOf(payee), 0);
        assertEq(c.totalWithdrawn(), 2 * PRICE);
        assertEq(_bal(address(c)), 0);
        _accounting();
    }

    function test_withdraw_errors() public {
        uint256 id = _register(PRICE);
        _pay(agent, id, _rid(agent, "1"), PRICE);
        vm.startPrank(payee);
        vm.expectRevert(HskPayPerRequest.ZeroAddress.selector);
        c.withdraw(address(0), 1);
        vm.expectRevert(HskPayPerRequest.ZeroAmount.selector);
        c.withdraw(payee, 0);
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.InsufficientCredit.selector, PRICE + 1, PRICE));
        c.withdraw(payee, PRICE + 1);
        vm.stopPrank();
        vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.InsufficientCredit.selector, 1, 0));
        vm.prank(provider); // the provider is not the payee: it has no credit
        c.withdraw(provider, 1);
        // withdrawing to the contract itself just strands the tokens as an uncredited balance; allowed by the token
        vm.prank(payee);
        c.withdraw(address(c), 1);
        assertEq(_bal(address(c)), PRICE, "token stayed, credit gone");
        assertEq(c.totalCredits(), PRICE - 1);
    }

    function test_withdraw_toBlacklistedAddressChangesNothing() public {
        uint256 id = _register(PRICE);
        _pay(agent, id, _rid(agent, "1"), PRICE);
        vm.prank(blacklister);
        usdc.blacklist(outsider);
        vm.expectRevert(bytes("Blacklistable: account is blacklisted"));
        vm.prank(payee);
        c.withdraw(outsider, PRICE);
        assertEq(c.creditOf(payee), PRICE);
        assertEq(c.totalWithdrawn(), 0);
        _accounting();
    }

    function test_withdraw_blacklistedPayeeCanStillWithdrawElsewhere() public {
        uint256 id = _register(PRICE);
        _pay(agent, id, _rid(agent, "1"), PRICE); // pull model: paying never touches the payee
        vm.prank(blacklister);
        usdc.blacklist(payee);
        uint256 before = _bal(outsider);
        vm.prank(payee); // the contract is the token sender, so the payee's own status does not block the payout
        c.withdraw(outsider, PRICE);
        assertEq(c.creditOf(payee), 0);
        assertEq(_bal(outsider) - before, PRICE);
    }

    /// POSITIVE CONTROL: a payee that re-enters withdraw() from the token hook is stopped by the guard.
    function test_withdraw_reentrancyIsBlocked() public {
        HookToken ht = new HookToken();
        HskPayPerRequest hc = new HskPayPerRequest(IERC20(address(ht)));
        ReentrantAccount rp = new ReentrantAccount(hc, ht);
        vm.prank(address(rp));
        hc.approveProvider(provider, true);
        vm.prank(provider);
        uint256 id = hc.registerEndpoint(PRICE, address(rp), "reentrant payee");
        ht.mint(agent, 10 * PRICE);
        vm.prank(agent);
        ht.approve(address(hc), type(uint256).max);
        vm.startPrank(agent);
        hc.pay(id, hc.requestIdFor(agent, keccak256("1")), PRICE);
        hc.pay(id, hc.requestIdFor(agent, keccak256("2")), PRICE);
        vm.stopPrank();
        rp.setMode(3); // bubble
        vm.expectRevert(abi.encodeWithSignature("ReentrancyGuardReentrantCall()"));
        rp.pull(address(rp), PRICE);
        assertEq(hc.creditOf(address(rp)), 2 * PRICE);
        rp.setMode(4); // swallow: only the outer withdrawal goes out
        rp.pull(address(rp), PRICE);
        assertTrue(rp.innerFailed());
        assertEq(rp.received(), PRICE);
        assertEq(hc.creditOf(address(rp)), PRICE);
        assertEq(ht.balanceOf(address(hc)), hc.totalCredits());
    }

    // ------------------------------------------------------------------ views
    function test_views_unknownIdsReturnZeros() public view {
        HskPayPerRequest.Endpoint memory e = c.getEndpoint(42);
        assertEq(e.provider, address(0));
        assertEq(e.price, 0);
        assertFalse(e.active);
        assertEq(bytes(c.nameOf(42)).length, 0);
        HskPayPerRequest.Payment memory p = c.getPayment(keccak256("nope"));
        assertEq(p.payer, address(0));
        assertEq(p.amount, 0);
        assertFalse(c.isPaid(keccak256("nope")));
        assertEq(c.creditOf(agent), 0);
        assertEq(c.endpointsOf(agent).length, 0);
        (bool ok, uint8 reason) = c.canPay(42, keccak256("nope"), agent, 1);
        assertFalse(ok);
        assertEq(reason, 1);
        assertEq(c.MAX_NAME(), 64);
    }

    /// Cross-language vector: the same numbers are checked by verifier/test (JS) and ops/test_ops.py (Python).
    function test_requestIdFor_offchainVector() public view {
        bytes32 domain = keccak256("hsk-paywall/v1");
        assertEq(domain, 0x4cc9139c98107575051bd6bb5f66ae90d3ff9e3e996f1cb12aa95e7d624e0d64);
        address payer = 0x2222222222222222222222222222222222222222;
        bytes32 resourceHash = keccak256("GET /v1/weather?city=Paris");
        bytes32 tok = 0x3333333333333333333333333333333333333333333333333333333333333333;
        bytes32 digest = keccak256(
            abi.encode(domain, uint256(177), address(0x1111111111111111111111111111111111111111), uint256(1), payer,
                resourceHash, tok)
        );
        assertEq(digest, 0x63cb1cdf9b0be81256dc25fadf023a41de8b27b5ce67d3960506edb07d765c95);
        assertEq(c.requestIdFor(payer, digest), 0x2222222222222222222222222222222222222222ce67d3960506edb07d765c95);
        assertEq(c.payerOf(c.requestIdFor(payer, digest)), payer);
    }

    // ------------------------------------------------------------------ fuzz
    function testFuzz_requestIdForRoundTrip(address payer, bytes32 digest) public view {
        bytes32 rid = c.requestIdFor(payer, digest);
        assertEq(c.payerOf(rid), payer);
        assertEq(uint256(rid) & type(uint96).max, uint256(digest) & type(uint96).max);
    }

    function testFuzz_payNeedsExactlyThePrice(uint128 price, uint256 sent) public {
        price = uint128(bound(price, 1, 1_000_000e6));
        sent = bound(sent, 0, 2_000_000e6);
        vm.prank(provider);
        uint256 id = c.registerEndpoint(price, payee, "fuzz");
        bytes32 rid = _rid(agent, "fuzz");
        _mint(agent, sent > 0 ? sent : 1);
        uint256 before = _bal(agent);
        if (sent != price) {
            vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.WrongAmount.selector, sent, price));
        }
        _pay(agent, id, rid, sent);
        assertEq(c.creditOf(payee), sent == price ? price : 0);
        assertEq(before - _bal(agent), sent == price ? price : 0);
        _accounting();
    }

    function testFuzz_authNeedsExactlyThePrice(uint128 price, uint256 signed) public {
        price = uint128(bound(price, 1, 1_000_000e6));
        signed = bound(signed, 0, 2_000_000e6);
        vm.prank(provider);
        uint256 id = c.registerEndpoint(price, payee, "fuzz-auth");
        bytes32 rid = _rid(agent, "fuzz-auth");
        _mint(agent, signed > 0 ? signed : 1);
        uint256 vb = block.timestamp + 300;
        bytes memory sig = _authSig(agentPk, id, rid, signed, vb);
        if (signed != price) {
            vm.expectRevert(abi.encodeWithSelector(HskPayPerRequest.WrongAmount.selector, signed, price));
        }
        c.payWithAuthorization(id, rid, agent, signed, 0, vb, sig);
        assertEq(c.creditOf(payee), signed == price ? price : 0);
        _accounting();
    }

    function testFuzz_canPayAgreesWithPay(uint8 endpointSel, uint8 ridSel, uint8 payerSel, uint8 amountSel, bool inactive)
        public
    {
        uint256 id = _register(PRICE);
        bytes32 used = _rid(agent, "used");
        _pay(agent, id, used, PRICE);
        if (inactive) {
            vm.prank(provider);
            c.setActive(id, false);
        }
        uint256 eid = endpointSel % 3 == 0 ? 0 : (endpointSel % 3 == 1 ? id : id + 1);
        bytes32[4] memory rids =
            [bytes32(0), used, _rid(agent, "fresh"), c.requestIdFor(address(0), keccak256("open"))];
        bytes32 rid = rids[ridSel % 4];
        address payer = payerSel % 2 == 0 ? agent : agent2;
        uint256[3] memory amounts = [uint256(0), uint256(PRICE), uint256(PRICE) + 1];
        uint256 amount = amounts[amountSel % 3];
        (bool ok, uint8 reason) = c.canPay(eid, rid, payer, amount);
        vm.prank(payer);
        (bool success, bytes memory ret) = address(c).call(abi.encodeCall(c.pay, (eid, rid, amount)));
        assertEq(success, ok, "canPay ok == pay success");
        if (!success) {
            bytes4 sel = bytes4(ret);
            bytes4[7] memory want = [
                bytes4(0),
                HskPayPerRequest.UnknownEndpoint.selector,
                HskPayPerRequest.EndpointInactive.selector,
                HskPayPerRequest.ZeroRequestId.selector,
                HskPayPerRequest.RequestNotForPayer.selector,
                HskPayPerRequest.AlreadyPaid.selector,
                HskPayPerRequest.WrongAmount.selector
            ];
            assertEq(sel, want[reason], "revert selector matches the canPay reason");
        } else {
            assertEq(reason, 0);
        }
    }
}
