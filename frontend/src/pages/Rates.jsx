import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Rates() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const [draft, setDraft] = useState({})
  const load = () => api.费率().then(setList)
  useEffect(() => {
    load()
  }, [])
  const edit = (r) =>
    setDraft({
      id: r.id,
      峰价: r.peak_price,
      平价: r.flat_price,
      谷价: r.valley_price,
    })
  const save = async () => {
    setMsg('')
    try {
      const r = await api.改档价(draft.id, {
        峰价: Number(draft.峰价),
        平价: Number(draft.平价),
        谷价: Number(draft.谷价),
      })
      setMsg(
        `费率「${r.名称}」已改档；${r.锁定电价不受影响会话数} 个会话的锁定电价保持不变（不回溯在充会话）`,
      )
      setDraft({})
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className="page">
      <h2>费率</h2>
      <p className="lead">
        峰平谷电价档，开会话时锁定当时档；改档价只影响此后新会话，在充/已关账会话的锁定电价冻结不回溯。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>峰价</th>
              <th>平价</th>
              <th>谷价</th>
              <th>启用</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td>{r.peak_price}</td>
                <td>{r.flat_price}</td>
                <td>{r.valley_price}</td>
                <td>{r.active ? '是' : '否'}</td>
                <td>
                  <button type="button" onClick={() => edit(r)}>
                    改档价
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {draft.id && (
          <div className="row">
            <label>峰价</label>
            <input
              value={draft.峰价}
              onChange={(e) => setDraft({ ...draft, 峰价: e.target.value })}
              style={{ width: 90 }}
            />
            <label>平价</label>
            <input
              value={draft.平价}
              onChange={(e) => setDraft({ ...draft, 平价: e.target.value })}
              style={{ width: 90 }}
            />
            <label>谷价</label>
            <input
              value={draft.谷价}
              onChange={(e) => setDraft({ ...draft, 谷价: e.target.value })}
              style={{ width: 90 }}
            />
            <button type="button" className="primary" onClick={save}>
              保存档价
            </button>
            <button type="button" onClick={() => setDraft({})}>
              取消
            </button>
          </div>
        )}
        <p className={`msg ${msg.includes('已改档') ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
