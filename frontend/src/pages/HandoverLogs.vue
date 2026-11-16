<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const watermarks = ref({})
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/handovers')
    const pairs = await Promise.all(
      rows.value.map(async (h) => {
        try {
          return [h.id, await getJson(`/api/handovers/${h.id}/escrow-watermark`)]
        } catch {
          return [h.id, null]
        }
      }),
    )
    watermarks.value = Object.fromEntries(pairs)
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <div>
    <h1>交班记录</h1>
    <p class="lead">每单交班留档：双方班次、清单批次数、最终状态，以及落库的托管水位证明。</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>单号</th><th>交班方</th><th>接班方</th><th>批次数</th><th>托管数</th>
            <th>状态</th><th>发起前/后在途（交班方→接班方）</th>
            <th>出口后在途（交班方→接班方）</th><th>出口后托管残留</th>
            <th>水位一致</th><th>开始</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="h in rows" :key="h.id">
            <td>#{{ h.id }}</td>
            <td>{{ h.from_shift_name }}</td>
            <td>{{ h.to_shift_name }}</td>
            <td>{{ h.batch_count }}</td>
            <td>{{ h.escrow_count }}</td>
            <td>{{ h.status }}</td>
            <template v-if="watermarks[h.id]">
              <td>
                {{ watermarks[h.id]['在途水位']['发起前']['交班方'] }}→{{ watermarks[h.id]['在途水位']['发起前']['接班方'] }}
                /
                {{ watermarks[h.id]['在途水位']['发起后']['交班方'] }}→{{ watermarks[h.id]['在途水位']['发起后']['接班方'] }}
              </td>
              <td v-if="watermarks[h.id]['在途水位']['出口后']">
                {{ watermarks[h.id]['在途水位']['出口后']['交班方'] }}→{{ watermarks[h.id]['在途水位']['出口后']['接班方'] }}
              </td>
              <td v-else>—</td>
              <td>{{ watermarks[h.id]['托管集合']['出口后残留'] ?? '—' }}</td>
              <td>
                <span class="tag" :class="{ on: watermarks[h.id].consistent }">
                  {{ watermarks[h.id].consistent ? '一致' : '不一致' }}
                </span>
              </td>
            </template>
            <template v-else>
              <td>—</td><td>—</td><td>—</td><td>—</td>
            </template>
            <td>{{ h.started_at }}</td>
            <td>{{ h.finished_at || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
