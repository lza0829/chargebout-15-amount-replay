import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Settle() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const load = () =>
    Promise.all([api.会话('充电中'), api.会话('已超时'), api.会话('结算中')]).then(
      ([a, b, c]) => setList([...a, ...b, ...c]),
    )
  useEffect(() => {
    load()
  }, [])
  const go = async (id) => {
    setMsg('')
    try {
      // 关账金额以关账瞬间按锁定电价×累计电量的重算值为准；
      // 预估列只供浏览，若被旁路改出分叉，后端整笔拒绝并回传拒因。
      const r = await api.结算(id)
      setMsg(
        `已关账：电量 ${r.电量}，锁定电价 ${r.锁定电价}，` +
          `重算落账 ${r.关账金额}（${r.算法}），与预估列一致`,
      )
      load()
    } catch (e) {
      setMsg(`关账被拒：${e.message}`)
    }
  }
  return (
    <div className="page">
      <h2>结算关账</h2>
      <p className="lead">
        关账当场按锁定电价与累计电量重算落账，预估金额列仅供浏览；旁路改预估会被整笔拒绝。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>锁定电价</th>
              <th>预估金额(浏览)</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.price_locked}</td>
                <td>{s.amount}</td>
                <td>
                  <button type="button" className="primary" onClick={() => go(s.id)}>
                    结算关账
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className={`msg ${msg.startsWith('已关账') ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
