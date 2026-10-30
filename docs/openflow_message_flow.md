# OpenFlow message flow for a new client request

Scenario: h1 (10.0.0.1, on s1) opens `http://10.0.0.100/` for the first time.
Strategy round_robin picks srv1 (10.0.0.11, on s5); the cheapest path is s1-s3-s5.

```mermaid
sequenceDiagram
    autonumber
    participant H1 as h1
    participant S1 as s1 (client edge)
    participant S3 as s3
    participant S5 as s5 (server edge)
    participant SRV as srv1
    participant C as Controller

    H1->>S1: ARP who-has 10.0.0.100
    S1->>C: PacketIn (table-miss)
    C->>S1: PacketOut ARP reply 10.0.0.100 is-at VIP_MAC (proxy ARP)
    S1->>H1: ARP reply

    H1->>S1: TCP SYN 10.0.0.1:40001 -> 10.0.0.100:80
    S1->>C: PacketIn (no matching rule)
    Note over C: DDoS check (new-flow rate, SYN count)<br/>strategy.select() -> srv1<br/>Dijkstra s1->s5 and s5->s1
    C->>S1: FlowMod reverse: src srv1:80 -> h1:40001, set src=VIP/VIP_MAC, out h1 port
    C->>S3: FlowMod reverse hop
    C->>S5: FlowMod reverse hop (ingress, SEND_FLOW_REM)
    C->>S5: FlowMod forward: dst srv1 -> out srv1 port
    C->>S3: FlowMod forward hop
    C->>S1: FlowMod forward: dst VIP -> set dst=srv1 IP/MAC, out to s3 (SEND_FLOW_REM)
    C->>S1: PacketOut the SYN with the same rewrite
    S1->>S3: SYN (dst 10.0.0.11)
    S3->>S5: SYN
    S5->>SRV: SYN
    SRV->>S5: SYN-ACK 10.0.0.11 -> 10.0.0.1 (matches reverse rule)
    S5->>S3: SYN-ACK
    S3->>S1: SYN-ACK
    S1->>H1: SYN-ACK rewritten to src 10.0.0.100
    Note over H1,SRV: Rest of the connection is switched in hardware, no controller involvement
    S1-->>C: FlowRemoved (after 10 s idle) -> registry entry dropped
```

Notes

1. **Egress first.** Rules are installed from the last hop back to the first, and
   the reverse direction before the forward one, so no packet of the connection
   can reach a switch that has no rule yet.
2. **Rewrite only at the client's edge switch.** The core sees real server
   addresses; the client only ever sees the VIP. OVS recomputes IP/TCP checksums.
3. **Server ARP.** When srv1 answers it needs h1's MAC; its ARP request reaches
   the controller, which answers from its host table (proxy ARP). No broadcast
   crosses an inter-switch link.
4. **Statistics.** Every 2 s the controller sends `OFPPortStatsRequest` and
   `OFPFlowStatsRequest` to every switch; the replies give link utilisation and
   per-connection packet counts (used for DDoS detection and active-flow counts).
5. **Link failure.** `OFPPortStatus` (link down) → controller deletes the rules of
   every path that used the link (`OFPFC_DELETE` by cookie), installs new paths,
   then sends `OFPBarrierRequest`; recovery time is measured to the last
   `OFPBarrierReply`.
6. **Health check.** The controller injects a TCP SYN from 10.0.0.254 to port 80 of
   each server with `OFPPacketOut`; the SYN-ACK (or RST) returns as `OFPPacketIn`.
