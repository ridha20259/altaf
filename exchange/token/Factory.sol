// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// Fixed-supply token created with ALTAF Exchange Token Factory. Whole supply minted to the owner, no mint function.
contract FactoryToken {
    string public name;
    string public symbol;
    uint8 public immutable decimals;
    uint256 public immutable totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(string memory n, string memory s, uint8 d, uint256 supply, address owner) {
        name = n; symbol = s; decimals = d; totalSupply = supply;
        balanceOf[owner] = supply;
        emit Transfer(address(0), owner, supply);
    }

    function _transfer(address from, address to, uint256 v) internal {
        require(to != address(0), "zero");
        uint256 b = balanceOf[from];
        require(b >= v, "balance");
        unchecked { balanceOf[from] = b - v; }
        balanceOf[to] += v;
        emit Transfer(from, to, v);
    }
    function transfer(address to, uint256 v) external returns (bool) { _transfer(msg.sender, to, v); return true; }
    function approve(address sp, uint256 v) external returns (bool) { allowance[msg.sender][sp] = v; emit Approval(msg.sender, sp, v); return true; }
    function transferFrom(address f, address t, uint256 v) external returns (bool) {
        uint256 a = allowance[f][msg.sender];
        require(a >= v, "allowance");
        if (a != type(uint256).max) allowance[f][msg.sender] = a - v;
        _transfer(f, t, v);
        return true;
    }
}
