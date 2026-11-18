@echo off
chcp 65001 >nul
echo ALTAF miner - ordinary computer (CPU)
if not exist address.txt (
  set /p ADDR=Paste your ALTAF address: 
  call echo %ADDR%> address.txt
)
set /p ADDR=<address.txt
altaf-miner.exe -network mainnet -node https://altafnet.187-126-9-68.sslip.io -address %ADDR% -algo cpu
pause
