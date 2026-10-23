// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

import {console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {HskPayPerRequest} from "../src/HskPayPerRequest.sol";
import {FiatTokenFixture} from "./Base.sol";

/// @dev Execution gas of the demo calls against the real USDC.e code (forge test --match-contract Gas -vv).
///      A real tx adds 21,000 + calldata.
contract HskPayPerRequestGas is FiatTokenFixture {
    HskPayPerRequest c;
    address provider = makeAddr("provider");
    uint256 pk = 0xA11CE;
    address agent;

    function _sig(uint256 id, bytes32 rid, uint256 vb) internal view returns (bytes memory) {
        return _signReceive(pk, address(c), 10_000, 0, vb, c.authorizationNonce(id, rid));
    }

    function test_gas_demoCalls() public {
        vm.warp(1_760_000_000);
        _loadUsdc();
        c = new HskPayPerRequest(IERC20(address(usdc)));
        agent = vm.addr(pk);
        _mint(agent, 10e6);
        vm.prank(agent);
        IERC20(address(usdc)).approve(address(c), type(uint256).max);
        uint256 g = gasleft();
        vm.prank(provider);
        uint256 id = c.registerEndpoint(10_000, provider, "GET /v1/hsk/address-risk");
        console.log("registerEndpoint", g - gasleft());
        bytes32 r1 = c.requestIdFor(agent, keccak256("q1"));
        bytes32 r2 = c.requestIdFor(agent, keccak256("q2"));
        bytes32 r3 = c.requestIdFor(agent, keccak256("q3"));
        g = gasleft();
        vm.prank(agent);
        c.pay(id, r1, 10_000);
        console.log("pay (approve flow, first payment)", g - gasleft());
        uint256 vb = block.timestamp + 300;
        bytes memory sig = _sig(id, r2, vb);
        g = gasleft();
        c.payWithAuthorization(id, r2, agent, 10_000, 0, vb, sig);
        console.log("payWithAuthorization (EIP-3009)", g - gasleft());
        sig = _sig(id, r3, vb);
        g = gasleft();
        c.payWithAuthorization(id, r3, agent, 10_000, 0, vb, sig);
        console.log("payWithAuthorization (warm)", g - gasleft());
        g = gasleft();
        vm.prank(provider);
        c.refund(r3);
        console.log("refund", g - gasleft());
        uint256 credit = c.creditOf(provider);
        g = gasleft();
        vm.prank(provider);
        c.withdraw(provider, credit);
        console.log("withdraw (all credit)", g - gasleft());
    }
}
