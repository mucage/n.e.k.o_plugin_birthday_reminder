import {
  Alert,
  Button,
  Card,
  DataTable,
  Field,
  Grid,
  Input,
  NumberInput,
  Page,
  Stack,
  StatCard,
  StatusBadge,
  Switch,
  Text,
  Toolbar,
  ToolbarGroup,
  useConfirm,
  useToast,
  useState,
  useEffect,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps } from "@neko/plugin-ui"

type Person = {
  id: string
  name: string
  birthday: string
  note?: string
  tags?: string[]
  tags_label?: string
  next_label?: string
  days_until?: number
  age?: number | null
  is_today?: boolean
  acked?: boolean
}

type Settings = {
  timezone?: string
  remind_after?: string
  quiet_after?: string
  repeat_minutes?: number
  max_reminders?: number
  gift_advance_days?: number
  context_cue?: boolean
  scan_conversations?: boolean
}

type ImportInfo = {
  at?: string
  filename?: string
  added?: number
  updated?: number
  skipped?: number
}

type PanelState = {
  now?: string
  timezone?: string
  people?: Person[]
  today?: Person[]
  upcoming?: Person[]
  settings?: Settings
  state?: {
    stopped?: boolean
    reminders?: number
    max_reminders?: number
    cue_sent?: boolean
    next_label?: string
  }
  stats?: {
    total?: number
    today?: number
    today_pending?: number
    upcoming_30?: number
  }
  last_import?: ImportInfo | null
  load_warning?: string | null
}

type FormState = {
  id: string
  name: string
  birthday: string
  note: string
  gift: boolean
  special: boolean
}

const emptyForm: FormState = {
  id: "",
  name: "",
  birthday: "",
  note: "",
  gift: false,
  special: false,
}

export default function BirthdayPanel(props: PluginSurfaceProps<PanelState>) {
  const { t } = props
  const state = props.state || {}
  const settings = state.settings || {}
  const stats = state.stats || {}
  const dayState = state.state || {}
  const people = Array.isArray(state.people) ? state.people : []
  const today = Array.isArray(state.today) ? state.today : []
  const [query, setQuery] = useState("")
  const [form, setForm] = useState<FormState>(emptyForm)
  const [formError, setFormError] = useState("")
  const [importMessage, setImportMessage] = useState("")
  const [importTone, setImportTone] = useState<"success" | "danger" | "warning">("success")
  const [importDetails, setImportDetails] = useState<string[]>([])
  const [settingsDraft, setSettingsDraft] = useState<Settings>(settings)
  const [savingSettings, setSavingSettings] = useState(false)
  const toast = useToast()
  const confirm = useConfirm()

  useEffect(() => {
    setSettingsDraft(settings)
  }, [state.settings])

  const filtered = query.trim()
    ? people.filter((person) => {
        const needle = query.trim().toLowerCase()
        return [person.name, person.birthday, person.note, person.tags_label]
          .some((value) => String(value || "").toLowerCase().includes(needle))
      })
    : people

  const resetForm = () => {
    setForm(emptyForm)
    setFormError("")
  }

  const startEdit = (person: Person) => {
    setForm({
      id: person.id,
      name: person.name,
      birthday: person.birthday,
      note: person.note || "",
      gift: (person.tags || []).includes("gift_advance"),
      special: (person.tags || []).includes("special_care"),
    })
    setFormError("")
  }

  const submitForm = async () => {
    if (!form.name.trim()) {
      setFormError(t("panel.form.nameRequired"))
      return
    }
    if (!form.birthday.trim()) {
      setFormError(t("panel.form.birthdayRequired"))
      return
    }
    const tags: string[] = []
    if (form.gift) tags.push("gift_advance")
    if (form.special) tags.push("special_care")
    setFormError("")
    try {
      if (form.id) {
        await props.api.call("update_person", {
          person_id: form.id,
          name: form.name,
          birthday: form.birthday,
          note: form.note,
          tags,
        })
        toast.success(t("panel.toast.updated"))
      } else {
        await props.api.call("add_person", {
          name: form.name,
          birthday: form.birthday,
          note: form.note,
          tags,
        })
        toast.success(t("panel.toast.added"))
      }
      resetForm()
      await props.api.refresh()
    } catch (error) {
      setFormError(error && error.message ? error.message : String(error))
    }
  }

  const deletePerson = async (person: Person) => {
    const accepted = await confirm({
      title: t("panel.actions.delete"),
      message: t("panel.actions.deleteConfirm", { name: person.name }),
      tone: "danger",
      confirmLabel: t("panel.actions.delete"),
      cancelLabel: t("panel.form.cancel"),
    })
    if (!accepted) return
    try {
      await props.api.call("delete_person", { person_id: person.id })
      toast.success(t("panel.toast.deleted"))
      await props.api.refresh()
    } catch (error) {
      toast.error(error && error.message ? error.message : String(error))
    }
  }

  const acknowledge = async (person: Person) => {
    try {
      await props.api.call("acknowledge", { person_id: person.id, action: "greeted" })
      toast.success(t("panel.toast.acked", { name: person.name }))
      await props.api.refresh()
    } catch (error) {
      toast.error(error && error.message ? error.message : String(error))
    }
  }

  const stopToday = async () => {
    try {
      await props.api.call("stop_today", {})
      toast.success(t("panel.toast.stopped"))
      await props.api.refresh()
    } catch (error) {
      toast.error(error && error.message ? error.message : String(error))
    }
  }

  const testReminder = async () => {
    try {
      await props.api.call("test_reminder", {})
      toast.success(t("panel.toast.testSent"))
    } catch (error) {
      toast.error(error && error.message ? error.message : String(error))
    }
  }

  const readBase64 = async (file: File): Promise<string> => {
    const buffer = await file.arrayBuffer()
    const bytes = new Uint8Array(buffer)
    let binary = ""
    const chunk = 0x8000
    for (let index = 0; index < bytes.length; index += chunk) {
      const slice = bytes.subarray(index, index + chunk)
      for (let offset = 0; offset < slice.length; offset += 1) {
        binary += String.fromCharCode(slice[offset])
      }
    }
    return btoa(binary)
  }

  const importFile = async (event: any) => {
    const input = event && event.target ? event.target : null
    const file: File | undefined = input && input.files ? input.files[0] : undefined
    if (!file) return
    if (file.size > 4 * 1024 * 1024) {
      setImportTone("danger")
      setImportMessage(t("panel.import.tooLarge"))
      if (input) input.value = ""
      return
    }
    setImportMessage(t("panel.import.importing"))
    setImportTone("warning")
    setImportDetails([])
    try {
      const content = await readBase64(file)
      const result = await props.api.call(
        "import_xlsx",
        { filename: file.name, content_base64: content },
        { timeoutMs: 120000 },
      )
      const payload = result && result.data ? result.data : (result && result.value ? result.value : result) || {}
      const added = payload && payload.added ? payload.added : 0
      const updated = payload && payload.updated ? payload.updated : 0
      const skipped = payload && payload.skipped ? payload.skipped : 0
      const errors = payload && Array.isArray(payload.errors) ? payload.errors : []
      const warnings = payload && Array.isArray(payload.warnings) ? payload.warnings : []
      setImportMessage(t("panel.import.result", { added, updated, skipped }))
      setImportTone(errors.length > 0 ? "warning" : "success")
      const details: string[] = []
      warnings.forEach((item: string) => details.push(String(item)))
      errors.slice(0, 8).forEach((item: any) => {
        details.push(t("panel.import.rowError", { row: item.row, message: item.message }))
      })
      setImportDetails(details)
      await props.api.refresh()
    } catch (error) {
      setImportTone("danger")
      setImportMessage(error && error.message ? error.message : String(error))
    } finally {
      if (input) input.value = ""
    }
  }

  const saveSettings = async () => {
    setSavingSettings(true)
    try {
      await props.api.call("save_settings", { settings: settingsDraft })
      toast.success(t("panel.settings.saved"))
      await props.api.refresh()
    } catch (error) {
      toast.error(error && error.message ? error.message : String(error))
    } finally {
      setSavingSettings(false)
    }
  }

  const tagBadges = (person: Person) => {
    const tags = person.tags || []
    return (
      <span>
        {tags.includes("gift_advance") ? <StatusBadge tone="info">{t("panel.tags.gift")}</StatusBadge> : null}
        {tags.includes("special_care") ? <StatusBadge tone="warning">{t("panel.tags.special")}</StatusBadge> : null}
      </span>
    )
  }

  return (
    <Page title={props.plugin.name || t("panel.title")} subtitle={t("panel.subtitle")}>
      {state.load_warning ? <Alert tone="warning">{state.load_warning}</Alert> : null}

      <Toolbar>
        <ToolbarGroup>
          <StatusBadge tone={dayState.stopped ? "warning" : "success"}>
            {dayState.stopped ? t("panel.today.stopped") : dayState.next_label || t("panel.today.waiting")}
          </StatusBadge>
        </ToolbarGroup>
        <ToolbarGroup>
          <Button onClick={() => props.api.refresh()}>{t("panel.actions.refresh")}</Button>
        </ToolbarGroup>
      </Toolbar>

      <Grid cols={4}>
        <StatCard label={t("panel.stats.total")} value={stats.total || 0} />
        <StatCard label={t("panel.stats.today")} value={stats.today || 0} />
        <StatCard label={t("panel.stats.pending")} value={stats.today_pending || 0} />
        <StatCard label={t("panel.stats.upcoming")} value={stats.upcoming_30 || 0} />
      </Grid>

      <Card title={t("panel.today.title")}>
        {today.length === 0 ? (
          <Text>{t("panel.today.empty")}</Text>
        ) : (
          <Stack>
            {today.map((person) => (
              <div key={person.id} className="birthday-today-row">
                <Stack>
                  <Text>
                    <strong>{person.name}</strong>
                    {person.age !== null && person.age !== undefined ? ` · ${t("panel.today.age", { age: person.age })}` : ""}
                    {person.note ? ` · ${person.note}` : ""}
                  </Text>
                  <Text>{tagBadges(person)}</Text>
                </Stack>
                {person.acked ? (
                  <StatusBadge tone="success">{t("panel.today.acked")}</StatusBadge>
                ) : (
                  <Button tone="success" onClick={() => acknowledge(person)}>
                    {t("panel.today.ack")}
                  </Button>
                )}
              </div>
            ))}
            <span>
              <Button tone="info" onClick={testReminder}>
                {t("panel.actions.test")}
              </Button>
              {dayState.stopped ? null : (
                <Button tone="warning" onClick={stopToday}>
                  {t("panel.today.stop")}
                </Button>
              )}
            </span>
          </Stack>
        )}
      </Card>

      <Card title={t("panel.people.title")}>
        <Stack>
          <Field label={t("panel.people.search")} help={t("panel.people.searchHelp")}>
            <Input
              value={query}
              placeholder={t("panel.people.searchPlaceholder")}
              onChange={(value) => setQuery(value)}
            />
          </Field>
          {filtered.length === 0 ? (
            <Text>{people.length === 0 ? t("panel.people.empty") : t("panel.people.noMatch")}</Text>
          ) : (
            <DataTable
              data={filtered}
              rowKey="id"
              columns={[
                { key: "name", label: t("panel.table.name") },
                { key: "birthday", label: t("panel.table.birthday") },
                { key: "next_label", label: t("panel.table.next") },
                { key: "note", label: t("panel.table.note") },
                {
                  key: "tags",
                  label: t("panel.table.tags"),
                  render: (person: Person) => tagBadges(person),
                },
                {
                  key: "actions",
                  label: t("panel.table.actions"),
                  render: (person: Person) => (
                    <span>
                      <Button onClick={() => startEdit(person)}>{t("panel.actions.edit")}</Button>
                      <Button tone="danger" onClick={() => deletePerson(person)}>
                        {t("panel.actions.delete")}
                      </Button>
                    </span>
                  ),
                },
              ]}
            />
          )}
        </Stack>
      </Card>

      <Grid cols={2}>
        <Card title={form.id ? t("panel.form.editTitle") : t("panel.form.addTitle")}>
          <Stack>
            {formError ? <Alert tone="danger">{formError}</Alert> : null}
            <Field label={t("panel.form.name")} required>
              <Input
                value={form.name}
                placeholder={t("panel.form.namePlaceholder")}
                onChange={(value) => setForm({ ...form, name: value })}
              />
            </Field>
            <Field label={t("panel.form.birthday")} help={t("panel.form.birthdayHelp")} required>
              <Input
                value={form.birthday}
                placeholder={t("panel.form.birthdayPlaceholder")}
                onChange={(value) => setForm({ ...form, birthday: value })}
              />
            </Field>
            <Field label={t("panel.form.note")}>
              <Input
                value={form.note}
                placeholder={t("panel.form.notePlaceholder")}
                onChange={(value) => setForm({ ...form, note: value })}
              />
            </Field>
            <Switch
              checked={form.gift}
              label={t("panel.form.tagGift")}
              onChange={(value) => setForm({ ...form, gift: value })}
            />
            <Switch
              checked={form.special}
              label={t("panel.form.tagSpecial")}
              onChange={(value) => setForm({ ...form, special: value })}
            />
            <span>
              <Button tone="success" onClick={submitForm}>
                {t("panel.form.save")}
              </Button>
              {form.id ? (
                <Button onClick={resetForm}>{t("panel.form.cancel")}</Button>
              ) : null}
            </span>
          </Stack>
        </Card>

        <Card title={t("panel.import.title")}>
          <Stack>
            <Text>{t("panel.import.help")}</Text>
            <Field label={t("panel.import.choose")}>
              <input type="file" accept=".xlsx,.xlsm,.csv" onChange={importFile} />
            </Field>
            {importMessage ? <Alert tone={importTone}>{importMessage}</Alert> : null}
            {importDetails.length > 0 ? (
              <Stack>
                {importDetails.map((line, index) => (
                  <Text key={index}>{line}</Text>
                ))}
              </Stack>
            ) : null}
            {state.last_import ? (
              <Text>
                {t("panel.import.last", {
                  time: state.last_import.at || "",
                  added: state.last_import.added || 0,
                  updated: state.last_import.updated || 0,
                })}
              </Text>
            ) : null}
          </Stack>
        </Card>
      </Grid>

      <Card title={t("panel.settings.title")}>
        <Stack>
          <Grid cols={2}>
            <Field label={t("panel.settings.remindAfter")} help={t("panel.settings.remindAfterHelp")}>
              <Input
                type="time"
                value={settingsDraft.remind_after || "08:30"}
                onChange={(value) => setSettingsDraft({ ...settingsDraft, remind_after: value })}
              />
            </Field>
            <Field label={t("panel.settings.quietAfter")} help={t("panel.settings.quietAfterHelp")}>
              <Input
                type="time"
                value={settingsDraft.quiet_after || "22:30"}
                onChange={(value) => setSettingsDraft({ ...settingsDraft, quiet_after: value })}
              />
            </Field>
            <Field label={t("panel.settings.repeat")} help={t("panel.settings.repeatHelp")}>
              <NumberInput
                value={settingsDraft.repeat_minutes || 60}
                min={5}
                max={720}
                onChange={(value) => setSettingsDraft({ ...settingsDraft, repeat_minutes: Number(value) || 60 })}
              />
            </Field>
            <Field label={t("panel.settings.max")} help={t("panel.settings.maxHelp")}>
              <NumberInput
                value={settingsDraft.max_reminders || 3}
                min={1}
                max={12}
                onChange={(value) => setSettingsDraft({ ...settingsDraft, max_reminders: Number(value) || 3 })}
              />
            </Field>
            <Field label={t("panel.settings.advance")} help={t("panel.settings.advanceHelp")}>
              <NumberInput
                value={settingsDraft.gift_advance_days || 7}
                min={0}
                max={60}
                onChange={(value) => setSettingsDraft({ ...settingsDraft, gift_advance_days: Number(value) || 0 })}
              />
            </Field>
            <Field label={t("panel.settings.timezone")} help={t("panel.settings.timezoneHelp")}>
              <Input
                value={settingsDraft.timezone || "Asia/Shanghai"}
                onChange={(value) => setSettingsDraft({ ...settingsDraft, timezone: value })}
              />
            </Field>
          </Grid>
          <Switch
            checked={settingsDraft.context_cue !== false}
            label={t("panel.settings.cue")}
            onChange={(value) => setSettingsDraft({ ...settingsDraft, context_cue: value })}
          />
          <Switch
            checked={settingsDraft.scan_conversations !== false}
            label={t("panel.settings.scan")}
            onChange={(value) => setSettingsDraft({ ...settingsDraft, scan_conversations: value })}
          />
          <span>
            <Button tone="success" disabled={savingSettings} onClick={saveSettings}>
              {t("panel.settings.save")}
            </Button>
          </span>
        </Stack>
      </Card>
    </Page>
  )
}
