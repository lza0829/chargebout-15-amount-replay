import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Audit() {
  const [rows, setRows] = useState([])
  const [sweep, setSweep] = useState(null)
  const [msg, setMsg] = useState('')
  const load = () =>
    Promise.all([api.金额审计(), api.审计巡检()]).then(([a, s]) => {
      setRows(a)
      setSweep(s)
    })
  useEffect(() => {
    load().catch((e) => setMsg(e.message))
  }, [])
  return (
    <div className="page">
      <h2>金额审计</h2>
      <p className="lead">
        全局唯一口径：锁定电价 × 累计电量（四舍五入到分）。每笔关账与拒关均留痕；
        巡检按同一算法全量重放，任何旁路篡改或两种算法混用都会被拧出来。
      </p>
      <div className="panel">
        <h3>重放巡检{sweep ? `（异常 ${sweep.异常数} 笔）` : ''}</h3>
        {sweep && sweep.异常数 === 0 && <p className="msg ok">全量闭合：预估列、落账、回执、详情、总览同口径。</p>}
        {sweep && sweep.异常数 > 0 && (
          <table>
            <thead>
              <tr>
                <th>会话号</th>
                <th>状态</th>
                <th>预估金额</th>
                <th>重放金额</th>
                <th>关账金额</th>
                <th>问题</th>
              </tr>
            </thead>
            <tbody>
              {sweep.异常明细.map((f) => (
                <tr key={f.会话编号}>
                  <td>{f.会话号}</td>
                  <td>{f.状态}</td>
                  <td>{f.预估金额}</td>
                  <td>{f.重放金额}</td>
                  <td>{f.关账金额 ?? '—'}</td>
                  <td>{f.问题}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="panel">
        <h3>审计流水</h3>
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>会话</th>
              <th>事件</th>
              <th>算法</th>
              <th>预估列</th>
              <th>重放</th>
              <th>落账</th>
              <th>结果</th>
              <th>备注</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id}>
                <td>{a.created_at}</td>
                <td>{a.session_id}</td>
                <td>{a.event}</td>
                <td>{a.algorithm}</td>
                <td>{a.shown_amount ?? '—'}</td>
                <td>{a.replay_amount}</td>
                <td>{a.settled_amount ?? '—'}</td>
                <td>
                  <span className={`tag ${a.result === '成功' ? '已关账' : '已中断'}`}>
                    {a.result}
                  </span>
                </td>
                <td>{a.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="msg">{msg}</p>
      </div>
    </div>
  )
}
