// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @dev EIP-3009 as implemented by Circle's FiatTokenV2_2 (USDC.e on HashKey Chain). `signature` is 65-byte r||s||v for
///      an EOA payer or any ERC-1271 signature for a smart-account payer.
interface IERC3009 {
    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes memory signature
    ) external;

    function authorizationState(address authorizer, bytes32 nonce) external view returns (bool);
}

/// @title HskPayPerRequest: an HTTP-402 paywall settled in USDC.e on HashKey Chain (Swift Bot Labs)
/// @notice A provider registers priced endpoints. A client (an agent, a backend) pays for ONE request, either
///         - pay(endpointId, requestId, amount): the client approved this contract and pays from its own wallet, or
///         - payWithAuthorization(...): the client only SIGNS an EIP-3009 ReceiveWithAuthorization for exactly the price
///           and anyone (the seller's server, a facilitator) submits it. The client needs no gas token at all.
///         The API server verifies the Paid event (endpointId, requestId, payer, amount) or getPayment(requestId)
///         before it answers.
/// Rules
///   - Both pay paths check, in this order (first failure = revert; canPay() returns the same code):
///     1 UnknownEndpoint  2 EndpointInactive  3 ZeroRequestId  4 RequestNotForPayer (the requestId is bound to another
///     payer)  5 AlreadyPaid (a requestId is paid at most once, ever, across all endpoints)  6 WrongAmount (the amount
///     the client agreed to must equal the price in force).
///   - Payer binding: the top 20 bytes of a requestId name the only address allowed to pay it (requestIdFor/payerOf);
///     a zero prefix means "unbound" (anyone may pay). A front-runner cannot burn someone else's bound requestId.
///   - Authorization binding: the EIP-3009 nonce must be authorizationNonce(endpointId, requestId), so a signed
///     authorization pays exactly one (endpoint, request) and nothing else. receiveWithAuthorization only accepts calls
///     from its `to` (this contract), so the signature cannot be redirected or front-run into a bare transfer.
///   - Pull payouts: a payment credits the endpoint's payee (creditOf); the payee withdraws whenever it wants. A
///     payment never sends tokens to the payee, so a payee can never block a client's payment.
///   - Price changes apply to new payments only: each payment records the amount it paid; a client holding an old
///     quote gets WrongAmount instead of being charged a different price.
///   - Refunds: the endpoint's provider can refund a paid requestId once. The amount comes out of the payee's
///     unwithdrawn credit and is sent to the payer. A refunded requestId stays used (it can never be paid again).
///   - The payee is fixed per endpoint (so a refund always debits the account that was credited). To change it,
///     register a new endpoint and deactivate the old one. A payee must consent: it is the provider itself, or it has
///     approved that provider with approveProvider() (so nobody can attach a stranger's address as payee and later
///     refund out of that stranger's credit).
/// Accounting: token.balanceOf(this) >= totalCredits == sum of all credits (equal unless someone sends tokens here
///             directly; such tokens are not credited to anyone and cannot be moved: there is no admin);
///             totalCredits + totalWithdrawn == totalPaid - totalRefunded.
/// No admin, no fees, no upgrades, no native coin (nothing payable, no receive()/fallback()).
contract HskPayPerRequest is ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ------------------------------------------------------------------ types
    struct Endpoint {
        address provider; // registered it; the only address that can change it or refund its payments
        bool active; // false: new payments revert EndpointInactive
        uint64 createdAt;
        address payee; // credited by every payment; fixed at registration
        uint64 paidCount; // payments received (refunded ones included)
        uint128 price; // exact amount of a new payment (token base units, > 0)
        uint128 totalPaid; // sum of all payments (refunded ones included)
        uint128 totalRefunded;
        uint64 refundCount;
    }

    struct Payment {
        address payer; // zero = not paid
        uint64 endpointId;
        bool refunded;
        uint128 amount; // the price in force when it was paid
        uint64 paidAt; // block.timestamp
        uint64 paidBlock; // block.number (lets a verifier count confirmations from the view alone)
    }

    /// @notice canPay()/pay() reason codes, in check order.
    enum Reason {
        OK,
        UnknownEndpoint,
        EndpointInactive,
        ZeroRequestId,
        RequestNotForPayer,
        AlreadyPaid,
        WrongAmount
    }

    // ------------------------------------------------------------------ constants and storage
    uint256 public constant MAX_NAME = 64; // bytes

    /// @notice The settlement token (USDC.e on HashKey Chain: EIP-3009, 6 decimals).
    IERC20 public immutable token;

    /// @notice Last endpoint id (ids start at 1).
    uint256 public endpointCount;
    /// @notice Sum of all credits; this contract's token balance is never below it.
    uint256 public totalCredits;
    uint256 public totalPaid;
    uint256 public totalRefunded;
    uint256 public totalWithdrawn;

    mapping(uint256 => Endpoint) internal _endpoints;
    mapping(uint256 => string) internal _names;
    mapping(address => uint256[]) internal _providerEndpoints;
    mapping(bytes32 => Payment) internal _payments;
    mapping(address => uint256) internal _credits;
    mapping(address => mapping(address => bool)) internal _approvedProviders; // payee => provider => approved

    // ------------------------------------------------------------------ errors
    error ZeroAddress();
    error NotAContract(address token);
    error InvalidPayee();
    error PayeeNotApproved(address payee);
    error InvalidPrice();
    error InvalidName();
    error UnknownEndpoint(uint256 id);
    error NotProvider();
    error EndpointInactive(uint256 id);
    error ZeroRequestId();
    error RequestNotForPayer(address boundPayer);
    error AlreadyPaid(bytes32 requestId);
    error WrongAmount(uint256 sent, uint256 price);
    error UnknownPayment(bytes32 requestId);
    error AlreadyRefunded(bytes32 requestId);
    error ZeroAmount();
    error InsufficientCredit(uint256 amount, uint256 credit);
    error BalanceMismatch(uint256 expected, uint256 received);

    // ------------------------------------------------------------------ events
    event EndpointRegistered(
        uint256 indexed id, address indexed provider, address indexed payee, uint256 price, string name
    );
    event PriceChanged(uint256 indexed id, uint256 oldPrice, uint256 newPrice);
    event ActiveSet(uint256 indexed id, bool active);
    event Paid(
        uint256 indexed endpointId, bytes32 indexed requestId, address indexed payer, address payee, uint256 amount
    );
    /// @notice Emitted right after Paid when the payment came from a signed EIP-3009 authorization; `submitter` is the
    ///         account that paid the gas (the seller's server or a facilitator).
    event PaidWithAuthorization(bytes32 indexed requestId, bytes32 indexed nonce, address indexed submitter);
    event Refunded(uint256 indexed endpointId, bytes32 indexed requestId, address indexed payer, uint256 amount);
    event Withdrawn(address indexed account, address indexed to, uint256 amount);
    event ProviderApproval(address indexed payee, address indexed provider, bool approved);

    constructor(IERC20 token_) {
        if (address(token_) == address(0)) revert ZeroAddress();
        if (address(token_).code.length == 0) revert NotAContract(address(token_));
        token = token_;
    }

    // ------------------------------------------------------------------ provider
    /// @notice Register a priced endpoint. msg.sender becomes its provider. Active immediately.
    function registerEndpoint(uint128 price, address payee, string calldata name)
        external
        nonReentrant
        returns (uint256 id)
    {
        if (payee == address(0)) revert ZeroAddress();
        if (payee == address(this)) revert InvalidPayee();
        if (payee != msg.sender && !_approvedProviders[payee][msg.sender]) revert PayeeNotApproved(payee);
        if (price == 0) revert InvalidPrice();
        uint256 n = bytes(name).length;
        if (n == 0 || n > MAX_NAME) revert InvalidName();
        id = ++endpointCount;
        Endpoint storage e = _endpoints[id];
        e.provider = msg.sender;
        e.active = true;
        e.createdAt = uint64(block.timestamp);
        e.payee = payee;
        e.price = price;
        _names[id] = name;
        _providerEndpoints[msg.sender].push(id);
        emit EndpointRegistered(id, msg.sender, payee, price, name);
    }

    /// @notice New price for NEW payments; payments already made keep the amount they paid.
    function setPrice(uint256 id, uint128 newPrice) external nonReentrant {
        Endpoint storage e = _provided(id);
        if (newPrice == 0) revert InvalidPrice();
        uint128 old = e.price;
        e.price = newPrice;
        emit PriceChanged(id, old, newPrice);
    }

    /// @notice Pause (false) or resume (true) new payments. Refunds and withdrawals are not affected.
    function setActive(uint256 id, bool active) external nonReentrant {
        Endpoint storage e = _provided(id);
        e.active = active;
        emit ActiveSet(id, active);
    }

    /// @notice Refund one paid request: its amount leaves the payee's credit and goes back to the payer. Once.
    function refund(bytes32 requestId) external nonReentrant {
        Payment storage p = _payments[requestId];
        if (p.payer == address(0)) revert UnknownPayment(requestId);
        Endpoint storage e = _endpoints[p.endpointId];
        if (msg.sender != e.provider) revert NotProvider();
        if (p.refunded) revert AlreadyRefunded(requestId);
        uint256 amount = p.amount;
        uint256 credit = _credits[e.payee];
        if (amount > credit) revert InsufficientCredit(amount, credit);
        p.refunded = true;
        e.totalRefunded += uint128(amount);
        e.refundCount += 1;
        _credits[e.payee] = credit - amount;
        totalCredits -= amount;
        totalRefunded += amount;
        address payer = p.payer;
        emit Refunded(p.endpointId, requestId, payer, amount);
        token.safeTransfer(payer, amount);
    }

    /// @notice As a payee: allow (or stop allowing) `provider` to register NEW endpoints that pay you. Existing endpoints
    ///         are not affected (you consented to them when they were registered).
    function approveProvider(address provider, bool approved) external nonReentrant {
        if (provider == address(0)) revert ZeroAddress();
        _approvedProviders[msg.sender][provider] = approved;
        emit ProviderApproval(msg.sender, provider, approved);
    }

    // ------------------------------------------------------------------ client
    /// @notice Pay for one request from your own wallet. `amount` is the price you agreed to (the 402 quote); it must
    ///         equal the price in force. Needs a prior token.approve(this, >= amount).
    function pay(uint256 endpointId, bytes32 requestId, uint256 amount) external nonReentrant {
        uint256 price = _checkAndRecord(endpointId, requestId, msg.sender, amount);
        uint256 before = token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender, address(this), price);
        _requireReceived(before, price);
    }

    /// @notice Pay for one request with a signed EIP-3009 ReceiveWithAuthorization from `from` (anyone may submit it).
    ///         The signed fields must be: from, to = this contract, value = `amount` (= the price in force), validAfter,
    ///         validBefore, nonce = authorizationNonce(endpointId, requestId).
    function payWithAuthorization(
        uint256 endpointId,
        bytes32 requestId,
        address from,
        uint256 amount,
        uint256 validAfter,
        uint256 validBefore,
        bytes calldata signature
    ) external nonReentrant {
        uint256 price = _checkAndRecord(endpointId, requestId, from, amount);
        bytes32 nonce = authorizationNonce(endpointId, requestId);
        uint256 before = token.balanceOf(address(this));
        IERC3009(address(token)).receiveWithAuthorization(
            from, address(this), price, validAfter, validBefore, nonce, signature
        );
        _requireReceived(before, price);
        emit PaidWithAuthorization(requestId, nonce, msg.sender);
    }

    // ------------------------------------------------------------------ payee
    /// @notice Withdraw `amount` of your credit (payments to endpoints where you are the payee) to `to`.
    function withdraw(address to, uint256 amount) external nonReentrant {
        if (to == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        uint256 credit = _credits[msg.sender];
        if (amount > credit) revert InsufficientCredit(amount, credit);
        _credits[msg.sender] = credit - amount;
        totalCredits -= amount;
        totalWithdrawn += amount;
        emit Withdrawn(msg.sender, to, amount);
        token.safeTransfer(to, amount);
    }

    // ------------------------------------------------------------------ views (never revert)
    function getEndpoint(uint256 id) external view returns (Endpoint memory) {
        return _endpoints[id];
    }

    function nameOf(uint256 id) external view returns (string memory) {
        return _names[id];
    }

    function endpointsOf(address provider) external view returns (uint256[] memory) {
        return _providerEndpoints[provider];
    }

    function getPayment(bytes32 requestId) external view returns (Payment memory) {
        return _payments[requestId];
    }

    function isPaid(bytes32 requestId) external view returns (bool) {
        return _payments[requestId].payer != address(0);
    }

    function creditOf(address account) external view returns (uint256) {
        return _credits[account];
    }

    /// @notice True when `provider` may register endpoints that pay `payee` (always true for payee == provider).
    function isProviderApproved(address payee, address provider) external view returns (bool) {
        return payee == provider || _approvedProviders[payee][provider];
    }

    /// @notice Same checks and order as pay()/payWithAuthorization(); reason codes as in `Reason`.
    function canPay(uint256 endpointId, bytes32 requestId, address payer, uint256 amount)
        external
        view
        returns (bool ok, uint8 reason)
    {
        (Reason r,,) = _check(endpointId, requestId, payer, amount);
        return (r == Reason.OK, uint8(r));
    }

    /// @notice The EIP-3009 nonce a payer must sign to pay `requestId` on `endpointId` through payWithAuthorization.
    function authorizationNonce(uint256 endpointId, bytes32 requestId) public view returns (bytes32) {
        return keccak256(abi.encode(address(this), endpointId, requestId));
    }

    /// @notice A requestId bound to `payer`: the payer's address in the top 20 bytes, the low 12 bytes of `digest`
    ///         below it. payer = address(0) gives an unbound requestId.
    function requestIdFor(address payer, bytes32 digest) public pure returns (bytes32) {
        return bytes32((uint256(uint160(payer)) << 96) | (uint256(digest) & type(uint96).max));
    }

    /// @notice The only address allowed to pay `requestId` (address(0) = anyone).
    function payerOf(bytes32 requestId) public pure returns (address) {
        return address(uint160(uint256(requestId) >> 96));
    }

    // ------------------------------------------------------------------ internal
    function _provided(uint256 id) internal view returns (Endpoint storage e) {
        if (id == 0 || id > endpointCount) revert UnknownEndpoint(id);
        e = _endpoints[id];
        if (msg.sender != e.provider) revert NotProvider();
    }

    /// @dev Checks, then records the payment and credits the payee (effects before the token interaction).
    function _checkAndRecord(uint256 endpointId, bytes32 requestId, address payer, uint256 amount)
        internal
        returns (uint256 price)
    {
        Reason r;
        address bound;
        (r, bound, price) = _check(endpointId, requestId, payer, amount);
        if (r != Reason.OK) _revertWith(r, endpointId, requestId, bound, amount, price);
        Endpoint storage e = _endpoints[endpointId];
        _payments[requestId] = Payment({
            payer: payer,
            endpointId: uint64(endpointId),
            refunded: false,
            amount: uint128(price),
            paidAt: uint64(block.timestamp),
            paidBlock: uint64(block.number)
        });
        e.paidCount += 1;
        e.totalPaid += uint128(price);
        address payee = e.payee;
        _credits[payee] += price;
        totalCredits += price;
        totalPaid += price;
        emit Paid(endpointId, requestId, payer, payee, price);
    }

    function _requireReceived(uint256 before, uint256 price) internal view {
        uint256 got = token.balanceOf(address(this)) - before;
        if (got != price) revert BalanceMismatch(price, got);
    }

    function _check(uint256 endpointId, bytes32 requestId, address payer, uint256 amount)
        internal
        view
        returns (Reason, address bound, uint256 price)
    {
        if (endpointId == 0 || endpointId > endpointCount) return (Reason.UnknownEndpoint, address(0), 0);
        Endpoint storage e = _endpoints[endpointId];
        price = e.price;
        if (!e.active) return (Reason.EndpointInactive, address(0), price);
        if (requestId == bytes32(0)) return (Reason.ZeroRequestId, address(0), price);
        bound = payerOf(requestId);
        if (bound != address(0) && bound != payer) return (Reason.RequestNotForPayer, bound, price);
        if (_payments[requestId].payer != address(0)) return (Reason.AlreadyPaid, bound, price);
        if (amount != price) return (Reason.WrongAmount, bound, price);
        return (Reason.OK, bound, price);
    }

    function _revertWith(Reason r, uint256 endpointId, bytes32 requestId, address bound, uint256 sent, uint256 price)
        internal
        pure
    {
        if (r == Reason.UnknownEndpoint) revert UnknownEndpoint(endpointId);
        if (r == Reason.EndpointInactive) revert EndpointInactive(endpointId);
        if (r == Reason.ZeroRequestId) revert ZeroRequestId();
        if (r == Reason.RequestNotForPayer) revert RequestNotForPayer(bound);
        if (r == Reason.AlreadyPaid) revert AlreadyPaid(requestId);
        revert WrongAmount(sent, price);
    }
}
