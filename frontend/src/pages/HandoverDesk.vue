<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const shifts = ref([])
const fromId = ref(1)
const toId = ref(2)
const current = ref(null)
const detail = ref(null)
const escrowRows = ref([])
const msg = ref('')
const ok = ref(false)

const watermark = computed(() => detail.value?.['托管水位'] || null)

async function refresh() {
  shifts.value = await getJson('/api/shifts')
  const logs = await getJson('/api/handovers')
  escrowRows.value = await getJson('/api/handovers/escrow')
  const active = logs.find((h) => h.status === '进行中') || null
  current.value = active ? await getJson(`/api/handovers/${active.id}`) : null
  // 进行中的单优先，其次最近一单：托管清单与水位证明与后端同源
  const target = current.value || (logs[0] ? await getJson(`/api/handovers/${logs[0].id}`) : null)
  detail.value = target
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
})

async function start() {
  msg.value = ''
  ok.value = false
  try {
    current.value = await postJson('/api/handovers/start', {
      from_shift_id: Number(fromId.value),
      to_shift_id: Number(toId.value),
    })
    ok.value = true
    msg.value = `已开交班单 #${current.value.id}：清单 ${current.value.batch_count} 批已进入托管，两侧在途均不计入`
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}

async function exitVia(path, successText, failText) {
  if (!current.value) return
  msg.value = ''
  ok.value = false
  try {
    const body = path.includes('cancel')
      ? { reason: '人工取消交班' }
      : path.includes('fail')
        ? { reason: '交班失败，批次退回交班方' }
        : {}
    const result = await postJson(`/api/handovers/${current.value.id}/${path}`, body)
    ok.value = result.status === '已完成'
    msg.value = result.status === '已完成'
      ? successText
      : `${failText}（交班单 #${result.id} 状态：${result.status}），清单批次已整单退回交班方在途`
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}
</script>

<template>
  <div>
    <h1>交班办理</h1>
    <p class="lead">
      发起后清单批次进入<strong>托管态</strong>：既不计入交班方在途，也不计入接班方在途，仅挂本交班单可查；
      托管期内禁止结清、改归属、再次交班。完成则出托管过户接班方，取消/失败则整单退回交班方。
    </p>

    <div class="panel">
      <div class="row">
        <label>交班方
          <select v-model.number="fromId">
            <option v-for="s in shifts" :key="s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}</option>
          </select>
        </label>
        <label>接班方
          <select v-model.number="toId">
            <option v-for="s in shifts" :key="'t'+s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}</option>
          </select>
        </label>
        <button class="btn" :disabled="!!current" @click="start">发起交班（进入托管）</button>
        <button class="btn ghost" :disabled="!current" @click="exitVia('complete', '已完成：托管批次出托管并过户接班方', '')">完成交班</button>
        <button class="btn ghost" :disabled="!current" @click="exitVia('cancel', '', '已取消')">取消交班</button>
        <button class="btn danger" :disabled="!current" @click="exitVia('fail', '', '已失败')">标记失败</button>
      </div>
      <p class="msg" :class="{ ok }">{{ msg }}</p>
    </div>

    <div class="panel" v-if="current">
      <h2>当前交班单 #{{ current.id }} · {{ current.status }}</h2>
      <p class="lead" style="margin-bottom:12px">
        {{ current.from_shift_name }} → {{ current.to_shift_name }} · 清单 {{ current.batch_count }} 批 ·
        托管中 {{ current.escrow_count }} 批
      </p>
      <table>
        <thead><tr><th>业务键</th><th>标题</th><th>批次状态</th><th>归属班次</th><th>托管于</th><th>进入托管时间</th></tr></thead>
        <tbody>
          <tr v-for="b in current.items || []" :key="b.id">
            <td>{{ b.biz_key }}</td>
            <td>{{ b.title }}</td>
            <td>
              <span class="tag" :class="{ escrow: b.status === '托管中' }">{{ b.status }}</span>
            </td>
            <td>{{ b.owner_shift_name || '—' }}</td>
            <td>{{ b.escrow_handover_id ? `交班单 #${b.escrow_handover_id}` : '—' }}</td>
            <td>{{ b.escrowed_at || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p class="lead" style="margin:12px 0 0">
        托管集合物理隔离于在途集合，独立入口 <code>GET /api/handovers/escrow?handover_id={{ current.id }}</code>
      </p>
    </div>

    <div class="panel" v-if="watermark">
      <h2>托管清单与水位证明 · 交班单 #{{ watermark.handover_id }}</h2>
      <p class="lead" style="margin-bottom:12px">
        落库证明：<code>GET /api/handovers/{{ watermark.handover_id }}/escrow-watermark</code> ·
        当前状态 {{ watermark.handover_status }} ·
        <span class="tag" :class="{ on: watermark.consistent }">{{ watermark.consistent ? '一致' : '不一致' }}</span>
        <span class="tag" :class="{ escrow: watermark['两侧在途均不含托管'] }">
          两侧在途均不含托管：{{ watermark['两侧在途均不含托管'] ? '是' : '否' }}
        </span>
      </p>
      <div class="stats">
        <div class="stat"><div class="k">发起前 · 交班方在途</div><div class="v">{{ watermark['在途水位']['发起前']['交班方'] }}</div></div>
        <div class="stat"><div class="k">发起前 · 接班方在途</div><div class="v">{{ watermark['在途水位']['发起前']['接班方'] }}</div></div>
        <div class="stat escrow-stat"><div class="k">进入托管</div><div class="v">{{ watermark['托管集合']['批次数'] }}</div></div>
        <div class="stat"><div class="k">发起后 · 交班方在途</div><div class="v">{{ watermark['在途水位']['发起后']['交班方'] }}</div></div>
        <div class="stat"><div class="k">发起后 · 接班方在途</div><div class="v">{{ watermark['在途水位']['发起后']['接班方'] }}</div></div>
        <div class="stat" v-if="watermark['在途水位']['出口后']">
          <div class="k">出口后 · 交班方在途</div>
          <div class="v">{{ watermark['在途水位']['出口后']['交班方'] }}</div>
        </div>
        <div class="stat" v-if="watermark['在途水位']['出口后']">
          <div class="k">出口后 · 接班方在途</div>
          <div class="v">{{ watermark['在途水位']['出口后']['接班方'] }}</div>
        </div>
        <div class="stat" v-if="watermark['托管集合']['出口后残留'] !== null">
          <div class="k">出口后托管残留</div>
          <div class="v">{{ watermark['托管集合']['出口后残留'] }}</div>
        </div>
      </div>
      <h3 style="font-size:14px;margin:4px 0 10px">托管清单（{{ watermark['托管当前'].length }} 批在托管）</h3>
      <table>
        <thead><tr><th>批次</th><th>业务键</th><th>标题</th><th>批次状态</th><th>归属班次</th><th>进入托管时间</th></tr></thead>
        <tbody>
          <tr v-for="e in watermark['托管当前']" :key="e.batch_id">
            <td>#{{ e.batch_id }}</td>
            <td>{{ e.biz_key }}</td>
            <td>{{ e.title }}</td>
            <td><span class="tag escrow">{{ e.batch_status }}</span></td>
            <td>{{ e.owner_shift_name || '—' }}</td>
            <td>{{ e.escrowed_at }}</td>
          </tr>
          <tr v-if="!watermark['托管当前'].length">
            <td colspan="6" class="muted-cell">已全部出托管，托管集合为空（出口后残留：{{ watermark['托管集合']['出口后残留'] ?? '—' }}）</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-else-if="escrowRows.length">
      <h2>托管中的批次（其他交班单）</h2>
      <table>
        <thead><tr><th>交班单</th><th>批次</th><th>标题</th><th>交班方</th><th>接班方</th></tr></thead>
        <tbody>
          <tr v-for="e in escrowRows" :key="`${e.handover_id}-${e.batch_id}`">
            <td>#{{ e.handover_id }}</td>
            <td>{{ e.biz_key }}</td>
            <td>{{ e.title }}</td>
            <td>{{ e.from_shift_name }}</td>
            <td>{{ e.to_shift_name }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
