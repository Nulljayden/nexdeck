import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FileCode2, FileWarning, RefreshCw, Trash2, Upload } from 'lucide-react'
import { useState, type ChangeEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { api, ApiError, del, get, post } from '../../api/client'
import { ServiceIcon } from '../../components/ServiceIcon'
import { Confirm, Dialog, Toast } from '../../components/ui'
import { SettingsCard } from './SettingsCard'

export interface ExtensionInfo {
  id: string
  kind: string
  name: string
  version: string
  description: string
  author: string
  icon: string
  category: string
  docs_url: string
  file: string
  connections: number
  widgets: { id: string; name: string; renderer: string }[]
}

interface ExtensionListing {
  folder: string
  extensions: ExtensionInfo[]
  broken: { file: string; error: string }[]
}

/**
 * Integrations the administrator brought in as files.
 *
 * An extension is a YAML file that describes a service's API: its connection
 * fields, how to sign in, and which cards read what from the answers. Once
 * imported it is an integration like any other, under System > Integrations.
 */
export function ExtensionsSettings() {
  const { t } = useTranslation()
  const queries = useQueryClient()
  const listing = useQuery({ queryKey: ['extensions'], queryFn: () => get<ExtensionListing>('/extensions') })
  const [pasting, setPasting] = useState(false)
  const [pasted, setPasted] = useState('')
  const [removing, setRemoving] = useState<ExtensionInfo | null>(null)
  const [source, setSource] = useState<{ name: string; text: string } | null>(null)
  const [toast, setToast] = useState<{ text: string; level: 'ok' | 'error' } | null>(null)

  const fail = (failure: unknown) => setToast({ text: failure instanceof ApiError ? failure.message : t('errors.network'), level: 'error' })
  const changed = () => {
    void listing.refetch()
    // The widget library and the connection sheet read the catalogue; an
    // extension that came or went changes it.
    void queries.invalidateQueries({ queryKey: ['adapters'] })
  }

  const importText = async (text: string) => {
    const made = await post<ExtensionInfo>('/extensions', { text })
    setToast({ text: t('settings.extensions.imported', { name: made.name, version: made.version }), level: 'ok' })
    changed()
  }

  const pick = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? [])
    event.target.value = ''
    for (const file of files) {
      try {
        await importText(await file.text())
      } catch (failure) {
        fail(failure)
        return
      }
    }
  }

  const items = listing.data?.extensions ?? []
  const broken = listing.data?.broken ?? []

  return (
    <>
      <SettingsCard title={t('settings.extensions.title')} description={t('settings.extensions.help')}>
        <div className="flex flex-wrap gap-2 mb-4">
          <label className="btn btn-accent cursor-pointer">
            <Upload size={14} />
            {t('settings.extensions.import')}
            <input type="file" multiple accept=".yaml,.yml" className="sr-only" onChange={(event) => void pick(event)} />
          </label>
          <button className="btn" onClick={() => setPasting((open) => !open)}>
            <FileCode2 size={14} />
            {t('settings.extensions.paste')}
          </button>
          <button
            className="btn ml-auto"
            onClick={() =>
              void post<ExtensionListing>('/extensions/reload')
                .then(() => {
                  changed()
                  setToast({ text: t('settings.extensions.reloaded'), level: 'ok' })
                })
                .catch(fail)
            }
          >
            <RefreshCw size={14} />
            {t('settings.extensions.reload')}
          </button>
        </div>

        {pasting && (
          <div className="mb-4">
            <textarea
              aria-label={t('settings.extensions.paste')}
              className="input font-mono text-xs"
              rows={10}
              value={pasted}
              placeholder={'id: my-service\nname: My service\nwidgets:\n  - id: status\n    ...'}
              onChange={(event) => setPasted(event.target.value)}
            />
            <div className="flex justify-end mt-2">
              <button
                className="btn btn-accent"
                disabled={!pasted.trim()}
                onClick={() =>
                  void importText(pasted)
                    .then(() => {
                      setPasted('')
                      setPasting(false)
                    })
                    .catch(fail)
                }
              >
                {t('settings.extensions.importPasted')}
              </button>
            </div>
          </div>
        )}

        {listing.isSuccess && items.length === 0 && <p className="text-sm text-muted">{t('settings.extensions.none')}</p>}

        <ul className="space-y-2">
          {items.map((one) => (
            <li key={one.id} className="rounded-xl border border-line p-3">
              <div className="flex items-start gap-3">
                <ServiceIcon icon={one.icon} size={28} className="flex-none mt-0.5" />
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-medium">{one.name}</span>
                    <span className="num text-xs text-faint">v{one.version}</span>
                    {one.author && <span className="text-xs text-faint">{t('settings.extensions.by', { author: one.author })}</span>}
                  </div>
                  {one.description && <p className="text-sm text-muted mt-0.5">{one.description}</p>}
                  <div className="flex flex-wrap gap-1 mt-2">
                    {one.widgets.map((widget) => (
                      <span key={widget.id} className="text-[11px] rounded-md bg-surface-hover px-1.5 py-0.5" title={widget.renderer}>
                        {widget.name}
                      </span>
                    ))}
                  </div>
                  <p className="text-[11px] text-faint mt-2">
                    {one.connections > 0 ? (
                      <Link to="/system/integrations" className="hover:text-accent">
                        {t('settings.extensions.connections', { count: one.connections })}
                      </Link>
                    ) : (
                      <Link to={`/system/integrations?add=${encodeURIComponent(one.kind)}`} className="hover:text-accent">
                        {t('settings.extensions.connect')}
                      </Link>
                    )}
                    {' · '}
                    <span className="num">{one.file}</span>
                  </p>
                </div>
                <div className="flex gap-1 flex-none">
                  <button
                    className="btn btn-icon h-7 w-7"
                    aria-label={t('settings.extensions.view')}
                    title={t('settings.extensions.view')}
                    onClick={() =>
                      // Plain text, not JSON, so the answer is read as it is.
                      void api<Response>(`/extensions/${one.id}/source`, { raw: true })
                        .then((response) => response.text())
                        .then((text) => setSource({ name: one.file, text }))
                        .catch(fail)
                    }
                  >
                    <FileCode2 size={14} />
                  </button>
                  <button className="btn btn-icon h-7 w-7 btn-danger" aria-label={t('common.delete')} title={t('common.delete')} onClick={() => setRemoving(one)}>
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>

        {broken.length > 0 && (
          <div className="mt-4">
            <h3 className="text-[11px] uppercase tracking-wide text-faint mb-1.5">{t('settings.extensions.broken')}</h3>
            <ul className="space-y-1.5">
              {broken.map((one) => (
                <li key={one.file} className="flex items-start gap-2 rounded-xl border border-bad/40 p-2.5 text-sm">
                  <FileWarning size={15} className="text-bad flex-none mt-0.5" />
                  <span className="min-w-0">
                    <span className="num font-medium">{one.file}</span>
                    <span className="block text-xs text-muted break-words">{one.error}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {listing.data && <p className="text-[11px] text-faint mt-4">{t('settings.extensions.folder', { folder: listing.data.folder })}</p>}
      </SettingsCard>

      <Confirm
        open={removing !== null}
        title={t('settings.extensions.remove', { name: removing?.name ?? '' })}
        body={removing && removing.connections > 0 ? t('settings.extensions.removeInUse', { count: removing.connections }) : t('settings.extensions.removeBody')}
        danger
        confirmLabel={t('common.delete')}
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          const target = removing
          setRemoving(null)
          if (!target) return
          void del(`/extensions/${target.id}`).then(changed).catch(fail)
        }}
      />

      <Dialog open={source !== null} onClose={() => setSource(null)} title={source?.name ?? ''} size="lg">
        <pre className="num text-xs whitespace-pre-wrap break-words max-h-[60vh] overflow-auto">{source?.text}</pre>
      </Dialog>

      {toast && (
        <Toast level={toast.level} onClose={() => setToast(null)}>
          {toast.text}
        </Toast>
      )}
    </>
  )
}
