export const reconcileViewTemplate = `
<div class="card wide" v-show="tab===4">
  <h2>物化对账</h2>
  <p>
    窗内事件 {{recon.window_events}} / 金额 {{recon.window_amount}} ·
    迟到桶 {{recon.late_bucket}} · 拒收 {{recon.rejects}} ·
    对齐 <b :class="recon.aligned?'ok':'late'">{{recon.aligned}}</b>
  </p>
  <p class="muted">迟到计数按 late_bucket.window_id 同源聚合，与迟到明细/汇总卡/窗口条带同态。</p>
  <table>
    <thead><tr><th>窗ID</th><th>窗口</th><th>状态</th><th>窗内笔数</th><th>迟到</th><th>对齐</th></tr></thead>
    <tbody>
      <tr v-for="r in reconAll" :key="r.window_id">
        <td>{{r.window_id}}</td>
        <td>{{r.name}}</td>
        <td>{{r.status}}</td>
        <td>{{r.window_events}}</td>
        <td :class="(r.late_bucket||0)>0?'late':''">{{r.late_bucket}}</td>
        <td :class="r.aligned?'ok':'late'">{{r.aligned}}</td>
      </tr>
    </tbody>
  </table>
</div>
`;
