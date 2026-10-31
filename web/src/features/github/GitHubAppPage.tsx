import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ExternalLink, FolderGit2, Link2, Search, ShieldCheck } from 'lucide-react';
import { api } from '../../api/client';
import { useLocale } from '../../app/locale';
import { Disclosure, EmptyState, ErrorState, LoadingState, PageHeader, RefreshButton, safeExternalUrl } from '../../components/workflow/Workflow';
import './github.css';

interface Installation { installation_id: number; account_login: string; account_type: string; repository_selection: string; manage_url: string; repositories: { id: number; full_name: string; html_url: string; visibility?: string; private?: boolean }[] }
interface Authorization { authorization: { status: string; github_username: string; installations: Installation[]; error_code: string | null }; authorization_url: string | null; install_url: string | null; repository_count: number }

export default function GitHubAppPage() {
  const { t } = useLocale();
  const query = useQuery({ queryKey: ['github-authorization'], queryFn: ({ signal }) => api<Authorization>('/api/v1/github-app', { signal }), gcTime: 0 });
  const data = query.data;
  const install = safeExternalUrl(data?.install_url);
  return <div><PageHeader title={t('GitHub 授权', 'GitHub authorization')} description={t('管理 Sakura AI 可以访问的账号和仓库。', 'Manage the accounts and repositories Sakura AI can access.')} actions={<RefreshButton busy={query.isFetching} onClick={() => void query.refetch()} />} />
    {query.isPending ? <LoadingState /> : query.isError || data?.authorization.status === 'error' ? <ErrorState retry={() => void query.refetch()} /> : data?.authorization.status === 'unconfigured' ? <EmptyState title={t('GitHub App 尚未配置', 'GitHub App is not configured')} description={t('请联系管理员完成 GitHub App 配置。', 'Ask your administrator to finish configuring the GitHub App.')} /> : <>
      <div className="github-connection"><span className="github-connection-icon"><ShieldCheck size={24} /></span><div><h2>{data?.authorization.status === 'connected' ? t('已连接 GitHub', 'Connected to GitHub') : t('连接你的 GitHub 账号', 'Connect your GitHub account')}</h2><p>{data?.authorization.status === 'connected' ? `@${data.authorization.github_username} · ${data.repository_count} ${t('个已授权仓库', 'authorized repositories')}` : t('授权后，可以查看你有权访问的 GitHub App 安装与仓库。', 'Authorize to view the GitHub App installations and repositories available to you.')}</p></div><div className="github-connection-actions">{data?.authorization_url && <a className="button primary" href={data.authorization_url}><Link2 size={16} />{data.authorization.status === 'connected' ? t('重新授权', 'Reauthorize') : t('连接 GitHub', 'Connect GitHub')}</a>}{install && <a className="button" href={install} target="_blank" rel="noreferrer">{t('授权更多仓库', 'Authorize repositories')}<ExternalLink size={15} /></a>}</div></div>
      {data?.authorization.installations.map(item => <InstallationView key={item.installation_id} installation={item} />)}
      {data?.authorization.status === 'connected' && !data.authorization.installations.length && <EmptyState title={t('还没有已授权安装', 'No authorized installations')} description={t('为你的账号或组织安装 GitHub App 后，刷新此页。', 'Install the GitHub App for your account or organization, then refresh this page.')} />}
    </>}
  </div>;
}

function InstallationView({ installation }: { installation: Installation }) {
  const { t } = useLocale();
  const [search, setSearch] = useState('');
  const manage = safeExternalUrl(installation.manage_url);
  return <section className="github-installation"><header><FolderGit2 size={21} /><div><h2>{installation.account_login}</h2><p>{installation.account_type === 'Organization' ? t('组织', 'Organization') : t('个人账号', 'Personal account')} · {installation.repository_selection === 'all' ? t('全部仓库', 'All repositories') : t('指定仓库', 'Selected repositories')}</p></div>{manage && <a className="button" href={manage} target="_blank" rel="noreferrer">{t('管理授权', 'Manage access')}<ExternalLink size={14} /></a>}</header><Disclosure title={t("查看 {0} 个仓库", "View {0} repositories", [installation.repositories.length])}><label className="search-field"><Search size={16} /><input type="search" aria-label={t('搜索已授权仓库', 'Search authorized repositories')} value={search} onChange={event => setSearch(event.target.value)} placeholder={t('搜索仓库…', 'Search repositories…')} /></label><div>{installation.repositories.filter(repo => repo.full_name.toLowerCase().includes(search.toLowerCase())).map(repo => <div className="github-repo" key={repo.id}>{safeExternalUrl(repo.html_url) ? <a href={safeExternalUrl(repo.html_url)} target="_blank" rel="noreferrer">{repo.full_name}</a> : <span>{repo.full_name}</span>}<span>{repo.visibility === 'internal' ? t('内部', 'Internal') : repo.private ? t('私有', 'Private') : t('公开', 'Public')}</span></div>)}</div></Disclosure></section>;
}
