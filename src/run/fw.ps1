# Create inbound allow rules for the DRG platform on the hospital internal LAN.
# Scope: only 192.0.2.0/24 and 198.51.100.0/24 (per 医务科 confirmed internal segment).
$s = '192.0.2.0/24','198.51.100.0/24'
New-NetFirewallRule -DisplayName "DRG Vite 5173 (internal)" -Direction Inbound -Protocol TCP -LocalPort 5173 -RemoteAddress $s -Action Allow -Profile Any -ErrorAction Stop
New-NetFirewallRule -DisplayName "DRG uvicorn 8000 (internal)" -Direction Inbound -Protocol TCP -LocalPort 8000 -RemoteAddress $s -Action Allow -Profile Any -ErrorAction Stop
Write-Host "firewall rules created"
