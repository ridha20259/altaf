// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract T {
    mapping(address => uint256) public balanceOf;
    uint256 public total;
    event Transfer(address indexed from, address indexed to, uint256 value);
    constructor(uint256 s) { balanceOf[msg.sender] = s; total = s; }
    function transfer(address to, uint256 v) external returns (bool) {
        require(balanceOf[msg.sender] >= v, "low");
        balanceOf[msg.sender] -= v; balanceOf[to] += v;
        emit Transfer(msg.sender, to, v);
        return true;
    }
    receive() external payable {}
    function bal() external view returns (uint256) { return address(this).balance; }
}
