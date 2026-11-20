export const lateViewTemplate = `
<div class="card wide" v-show="tab===2">
  <h2>迟到桶</h2>
  <p class="muted">
    甲路径：桶行挂存活 window_id 外键，归属册当天锁定，不按窗名串桶；
    删窗同事务拒删或级联迁移，孤儿一律为 0。
  </p>
  <p>
    合计 {{lateSummary.count||0}} 笔 / 金额 {{lateSummary.amount||0}}
    · 孤儿桶 <b :class="(lateSummary.orphan_buckets||0)===0?'ok':'late'">{{lateSummary.orphan_buckets||0}}</b>
    · 检疫 {{lateSummary.quarantined_buckets||0}}
    · 归属册路径 {{attribution.active_path||'-'}}（{{attribution.path_name||'-'}}）
  </p>
  <table>
    <thead><tr><th>归属窗ID</th><th>窗口（存活名）</th><th>键</th><th>时间</th><th>金额</th><th>原因</th><th>操作</th></tr></thead>
    <tbody>
      <tr v-for="e in lateItems" :key="'l'+e.id">
        <td>{{e.window_id}}</td>
        <td>{{e.window_name_live || e.window_name}}</td>
        <td>{{e.biz_key}}</td>
        <td>{{e.event_ts}}</td>
        <td>{{e.amount}}</td>
        <td>{{e.reason}}</td>
        <td>
          <button class="ghost" @click="promoteLate(e.id)">补入空桩</button>
          <button class="ghost" @click="discardLate(e.id)">作废空桩</button>
        </td>
      </tr>
    </tbody>
  </table>
  <h3 style="margin-top:14px">按窗汇总（与明细同源 window_id）</h3>
  <table>
    <thead><tr><th>窗ID</th><th>窗名</th><th>笔数</th><th>金额</th></tr></thead>
    <tbody>
      <tr v-for="b in (lateSummary.by_window||[])" :key="'b'+b.window_id">
        <td>{{b.window_id}}</td><td>{{b.window_name}}</td><td>{{b.c}}</td><td>{{b.s}}</td>
      </tr>
    </tbody>
  </table>
</div>
`;
