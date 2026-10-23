// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {HskPayPerRequest} from "../src/HskPayPerRequest.sol";

/// @dev The subset of Circle's FiatTokenV2_2 the tests drive (admin calls included).
interface IFiatToken {
    function initialize(
        string memory tokenName,
        string memory tokenSymbol,
        string memory tokenCurrency,
        uint8 tokenDecimals,
        address newMasterMinter,
        address newPauser,
        address newBlacklister,
        address newOwner
    ) external;
    function initializeV2(string calldata newName) external;
    function initializeV2_1(address lostAndFound) external;
    function initializeV2_2(address[] calldata accountsToBlacklist, string calldata newSymbol) external;
    function configureMinter(address minter, uint256 minterAllowedAmount) external returns (bool);
    function mint(address to, uint256 amount) external returns (bool);
    function blacklist(address account) external;
    function unBlacklist(address account) external;
    function pause() external;
    function unpause() external;
    function DOMAIN_SEPARATOR() external view returns (bytes32);
    function RECEIVE_WITH_AUTHORIZATION_TYPEHASH() external view returns (bytes32);
    function TRANSFER_WITH_AUTHORIZATION_TYPEHASH() external view returns (bytes32);
    function authorizationState(address authorizer, bytes32 nonce) external view returns (bool);
    function cancelAuthorization(address authorizer, bytes32 nonce, bytes memory signature) external;
    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes memory signature
    ) external;
    function transferWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes memory signature
    ) external;
    function name() external view returns (string memory);
    function version() external view returns (string memory);
    function decimals() external view returns (uint8);
}

/// @dev Loads the REAL FiatTokenV2_2 runtime code (eth_getCode of USDC.e's implementation on HashKey Chain,
///      0x82f67Bdea6CbCC6BD34526073300e619225b22DE, verified on hsk.blockscout.com) and initializes it the way a fresh
///      deployment is, so every token rule (EIP-3009 checks, blacklist, pause, allowance) is the production one.
abstract contract FiatTokenFixture is Test {
    address internal constant USDC_ADDR = 0x054ed45810DbBAb8B27668922D110669c9D88D0a; // same address as on 177
    address internal constant SIG_LIB = 0x729f6fa515c8024F489c62CED77aF8049dFD60F7; // verified "SignatureChecker" on 177
    address internal tokenOwner = makeAddr("tokenOwner");
    address internal masterMinter = makeAddr("masterMinter");
    address internal minter = makeAddr("minter");
    address internal blacklister = makeAddr("blacklister");
    address internal pauser = makeAddr("pauser");
    IFiatToken internal usdc;

    function _loadUsdc() internal {
        bytes memory code = vm.parseBytes(vm.readFile("test/fixtures/FiatTokenV2_2.runtime.hex"));
        assertEq(code.length, 23464, "fixture size");
        assertEq(keccak256(code), 0xfa66f3389341145012aeb69dedd0329994caf963264084abb51d30e1cbd2aa68, "fixture = eth_getCode on 177");
        vm.etch(USDC_ADDR, code);
        // FiatTokenV2_2 links Circle's SignatureChecker as an external library (DELEGATECALL) at this address on 177
        bytes memory lib = vm.parseBytes(vm.readFile("test/fixtures/SignatureChecker.runtime.hex"));
        assertEq(keccak256(lib), 0xf1d90fdb4120c02275247097cbf8499246a44e58cc6569cce56877a2e1a7945f, "lib = eth_getCode on 177");
        vm.etch(SIG_LIB, lib);
        usdc = IFiatToken(USDC_ADDR);
        usdc.initialize("Bridged USDC", "USDC.e", "USD", 6, masterMinter, pauser, blacklister, tokenOwner);
        usdc.initializeV2("Bridged USDC");
        usdc.initializeV2_1(tokenOwner);
        usdc.initializeV2_2(new address[](0), "USDC.e");
        vm.prank(masterMinter);
        usdc.configureMinter(minter, type(uint256).max);
    }

    function _mint(address to, uint256 amount) internal {
        vm.prank(minter);
        usdc.mint(to, amount);
    }

    function _bal(address a) internal view returns (uint256) {
        return IERC20(address(usdc)).balanceOf(a);
    }

    function _receiveDigest(address from, address to, uint256 value, uint256 validAfter, uint256 validBefore, bytes32 nonce)
        internal
        view
        returns (bytes32)
    {
        bytes32 structHash = keccak256(
            abi.encode(usdc.RECEIVE_WITH_AUTHORIZATION_TYPEHASH(), from, to, value, validAfter, validBefore, nonce)
        );
        return keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash));
    }

    function _signReceive(
        uint256 pk,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce
    ) internal view returns (bytes memory) {
        address from = vm.addr(pk);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, _receiveDigest(from, to, value, validAfter, validBefore, nonce));
        return abi.encodePacked(r, s, v);
    }

    function _signTransfer(uint256 pk, address to, uint256 value, uint256 validAfter, uint256 validBefore, bytes32 nonce)
        internal
        view
        returns (bytes memory)
    {
        address from = vm.addr(pk);
        bytes32 structHash = keccak256(
            abi.encode(usdc.TRANSFER_WITH_AUTHORIZATION_TYPEHASH(), from, to, value, validAfter, validBefore, nonce)
        );
        bytes32 d = keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, d);
        return abi.encodePacked(r, s, v);
    }
}

/// @dev A smart-account payer (an agent's wallet contract): holds USDC.e, pays with pay() through approve, and signs
///      EIP-3009 authorizations through ERC-1271 with its owner key.
contract SmartAccount {
    address public immutable owner;
    bytes4 internal constant MAGIC = 0x1626ba7e;

    constructor(address owner_) {
        owner = owner_;
    }

    function exec(address to, bytes calldata data) external returns (bytes memory) {
        require(msg.sender == owner, "not owner");
        (bool ok, bytes memory ret) = to.call(data);
        if (!ok) {
            assembly {
                revert(add(ret, 32), mload(ret))
            }
        }
        return ret;
    }

    function isValidSignature(bytes32 hash, bytes calldata sig) external view returns (bytes4) {
        if (sig.length != 65) return 0xffffffff;
        bytes32 r = bytes32(sig[0:32]);
        bytes32 s = bytes32(sig[32:64]);
        uint8 v = uint8(sig[64]);
        return ecrecover(hash, v, r, s) == owner ? MAGIC : bytes4(0xffffffff);
    }
}

interface ITokenHook {
    function onTokenReceived(address from, uint256 amount) external;
}

/// @dev POSITIVE CONTROL ONLY: an ERC-20 that calls the recipient back on every transfer (like ERC-777 hooks). USDC.e
///      has no hooks; this token exists so the reentrancy guard is proven to work, not assumed.
contract HookToken {
    string public constant name = "Hook";
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => bool) public hooked; // only accounts that opt in get the callback

    function registerHook() external {
        hooked[msg.sender] = true;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _move(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(allowance[from][msg.sender] >= amount, "allowance");
        allowance[from][msg.sender] -= amount;
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "balance");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        if (hooked[to]) ITokenHook(to).onTokenReceived(from, amount);
    }
}

/// @dev A payer/payee for HookToken that re-enters the paywall when it receives tokens.
///      mode 0 accept, 1 re-enter refund (bubble), 2 re-enter refund (swallow), 3 re-enter withdraw (bubble),
///      4 re-enter withdraw (swallow), 5 re-enter pay (bubble).
contract ReentrantAccount is ITokenHook {
    HskPayPerRequest public immutable c;
    HookToken public immutable t;
    uint8 public mode;
    bytes32 public lastRequest;
    uint256 public lastEndpoint;
    bool public innerFailed;
    uint256 public received;

    constructor(HskPayPerRequest c_, HookToken t_) {
        c = c_;
        t = t_;
        t_.approve(address(c_), type(uint256).max);
        t_.registerHook();
    }

    function setMode(uint8 m) external {
        mode = m;
    }

    function pay(uint256 endpointId, bytes32 requestId, uint256 amount) external {
        lastRequest = requestId;
        lastEndpoint = endpointId;
        c.pay(endpointId, requestId, amount);
    }

    function pull(address to, uint256 amount) external {
        c.withdraw(to, amount);
    }

    function onTokenReceived(address, uint256 amount) external {
        require(msg.sender == address(t), "only token");
        received += amount;
        uint8 m = mode;
        mode = 0; // one level only
        if (m == 1) {
            c.refund(lastRequest);
        } else if (m == 2) {
            try c.refund(lastRequest) {} catch { innerFailed = true; }
        } else if (m == 3) {
            c.withdraw(address(this), c.creditOf(address(this)));
        } else if (m == 4) {
            try c.withdraw(address(this), c.creditOf(address(this))) {} catch { innerFailed = true; }
        } else if (m == 5) {
            c.pay(lastEndpoint, bytes32(uint256(uint160(address(this))) << 96 | 0xbeef), amount);
        }
    }
}
