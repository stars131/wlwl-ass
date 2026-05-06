import { useState } from 'react';

import { previewProposal, type ProposalPreview, type SkillProposal } from '../api/skillsApi';
import { useDecideProposal, useSkillProposals } from '../hooks/useSkills';

/**
 * Skill self-improvement proposal queue (#6).
 *
 * Lists every proposal the agent has filed via ``skill_propose_patch`` so
 * the user can read the diff + reason and accept / reject. Accepts apply
 * the diff to ``memory/<skill>_sop.md`` and back the original up to
 * ``memory/skill_backups/``; rejects just close the proposal.
 *
 * Open proposals get top-of-list priority; recent decisions stay visible
 * for context. Polling interval is 10s — fast enough that an agent that
 * just filed a proposal sees it appear without manual refresh.
 */
export function ProposalsCard(): JSX.Element {
  const proposals = useSkillProposals();
  const decide = useDecideProposal();
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const rows = proposals.data?.proposals ?? [];
  const open = rows.filter((p) => p.status === 'open');
  const recent = rows.filter((p) => p.status !== 'open').slice(0, 8);

  const handleDecide = (p: SkillProposal, decision: 'accept' | 'reject') => {
    if (decision === 'accept' && !window.confirm(
      `应用 diff 到 ${p.skill_path ?? p.skill_id}？原文件会自动备份到 memory/skill_backups/`,
    )) return;
    decide.mutate({ id: p.id, decision });
  };

  return (
    <section className="rounded-md border border-border bg-card p-3 flex flex-col gap-3">
      <header className="flex items-baseline justify-between gap-2 flex-wrap">
        <div>
          <h2 className="text-sm font-semibold">Skill 改进提案</h2>
          <p className="text-xs text-muted-foreground">
            agent 通过 ``skill_propose_patch`` 提交的改动队列。accept 会应用 diff 并自动备份原文件。
          </p>
        </div>
        <span className="text-xs text-muted-foreground">
          {open.length} 待审 · {recent.length} 已决
        </span>
      </header>

      {proposals.isLoading ? (
        <p className="text-xs text-muted-foreground">加载中…</p>
      ) : null}
      {proposals.error ? (
        <p className="text-xs text-destructive">加载失败：{String(proposals.error)}</p>
      ) : null}

      {open.length === 0 && !proposals.isLoading ? (
        <p className="text-xs text-muted-foreground italic">没有待审提案。</p>
      ) : null}

      <ul className="flex flex-col gap-2">
        {open.map((p) => (
          <ProposalRow
            key={p.id}
            proposal={p}
            expanded={expandedId === p.id}
            onToggle={() => setExpandedId((cur) => (cur === p.id ? null : p.id))}
            onDecide={handleDecide}
            isPending={decide.isPending}
          />
        ))}
      </ul>

      {recent.length > 0 ? (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">最近已决（{recent.length}）</summary>
          <ul className="mt-2 flex flex-col gap-1">
            {recent.map((p) => (
              <li
                key={p.id}
                className="rounded border border-border bg-muted/40 px-2 py-1 flex items-center gap-2 text-[11px]"
              >
                <span className={
                  p.status === 'accepted'
                    ? 'text-emerald-600'
                    : 'text-destructive'
                }>
                  {p.status === 'accepted' ? '✅ 已应用' : '❌ 已拒绝'}
                </span>
                <span className="font-mono truncate flex-1">{p.skill_id}</span>
                <span className="text-muted-foreground">{p.decided_at ?? ''}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {decide.isError ? (
        <p className="text-xs text-destructive">操作失败：{String(decide.error)}</p>
      ) : null}
      {decide.data && decide.data.ok === false ? (
        <p className="text-xs text-destructive">操作失败：{decide.data.message}</p>
      ) : null}
    </section>
  );
}

interface RowProps {
  proposal: SkillProposal;
  expanded: boolean;
  onToggle: () => void;
  onDecide: (p: SkillProposal, decision: 'accept' | 'reject') => void;
  isPending: boolean;
}

function ProposalRow({ proposal, expanded, onToggle, onDecide, isPending }: RowProps): JSX.Element {
  const [preview, setPreview] = useState<ProposalPreview | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewErr, setPreviewErr] = useState<string | null>(null);

  const onPreview = async () => {
    if (preview) {
      // Toggle off
      setPreview(null);
      return;
    }
    setPreviewBusy(true);
    setPreviewErr(null);
    try {
      const data = await previewProposal(proposal.id);
      setPreview(data);
    } catch (err) {
      setPreviewErr(String(err));
    } finally {
      setPreviewBusy(false);
    }
  };

  return (
    <li className="rounded border border-border bg-background p-2 flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <div className="min-w-0">
          <span className="font-mono text-xs truncate">{proposal.skill_id}</span>
          <span className="ml-2 text-[11px] text-muted-foreground">{proposal.proposed_at}</span>
        </div>
        <div className="flex gap-1 shrink-0">
          <button
            type="button"
            onClick={onToggle}
            className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent"
          >
            {expanded ? '收起' : '查看 diff'}
          </button>
          <button
            type="button"
            onClick={onPreview}
            disabled={previewBusy}
            className="px-2 py-0.5 text-xs rounded border border-border hover:bg-accent disabled:opacity-50"
            title="dry-run apply：在不写文件的前提下显示 diff 应用后的结果"
          >
            {previewBusy ? '预览中…' : preview ? '关闭预览' : '预览结果'}
          </button>
          <button
            type="button"
            onClick={() => onDecide(proposal, 'accept')}
            disabled={isPending}
            className="px-2 py-0.5 text-xs rounded bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            采纳
          </button>
          <button
            type="button"
            onClick={() => onDecide(proposal, 'reject')}
            disabled={isPending}
            className="px-2 py-0.5 text-xs rounded border border-destructive/40 text-destructive hover:bg-destructive/10 disabled:opacity-50"
          >
            拒绝
          </button>
        </div>
      </div>
      {proposal.reason ? (
        <p className="text-xs text-muted-foreground">{proposal.reason}</p>
      ) : null}
      {previewErr ? (
        <p className="text-xs text-destructive">预览失败：{previewErr}</p>
      ) : null}
      {expanded ? (
        <pre className="text-[11px] font-mono bg-muted/40 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all max-h-80">
          {proposal.diff}
        </pre>
      ) : null}
      {preview ? (
        <div className="rounded border border-border bg-muted/40 p-2 flex flex-col gap-1.5">
          <p className={`text-[11px] ${preview.ok ? 'text-emerald-600' : 'text-destructive'}`}>
            {preview.ok
              ? `✅ dry-run OK · ${preview.mode}: ${preview.message}`
              : `❌ dry-run failed: ${preview.error || preview.message || 'unknown'}`}
          </p>
          {preview.ok && preview.after !== undefined ? (
            <pre className="text-[11px] font-mono bg-background rounded p-2 overflow-x-auto whitespace-pre-wrap break-all max-h-80">
              {preview.after}
            </pre>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
