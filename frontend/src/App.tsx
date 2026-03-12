import { useEffect, useState } from 'react';
import axios from 'axios';
import SearchBar from './components/SearchBar';
import type { ResearchMode } from './components/SearchBar';
import StatusMonitor from './components/StatusMonitor';
import type { PublishStatus, TaskStatus } from './components/StatusMonitor';
import ReportViewer from './components/ReportViewer';

const API_BASE_URL = 'http://localhost:8000';

function App() {
  const [taskId, setTaskId] = useState<string | null>(null);
  const [status, setStatus] = useState<TaskStatus>(null);
  const [detail, setDetail] = useState<string>('');
  const [report, setReport] = useState<string>('');
  const [researchMode, setResearchMode] = useState<ResearchMode>('medium');
  const [llmCostRmb, setLlmCostRmb] = useState<number>(0);
  const [externalCostUsd, setExternalCostUsd] = useState<number>(0);
  const [tavilyCredits, setTavilyCredits] = useState<number>(0);
  const [publishStatus, setPublishStatus] = useState<PublishStatus>('');

  useEffect(() => {
    let interval: ReturnType<typeof setInterval>;

    if (taskId && (status === 'PENDING' || status === 'QUEUED' || status === 'STARTED')) {
      interval = setInterval(async () => {
        try {
          const res = await axios.get(`${API_BASE_URL}/research/${taskId}`);
          const data = res.data;

          setStatus(data.status as TaskStatus);
          setResearchMode((data.research_mode || 'medium') as ResearchMode);
          setLlmCostRmb(Number(data.llm_cost_rmb || 0));
          setExternalCostUsd(Number(data.external_cost_usd_est || 0));
          setTavilyCredits(Number(data.tavily_credits_est || 0));
          setPublishStatus((data.publish_status || '') as PublishStatus);

          if (data.status === 'SUCCESS') {
            setDetail('\u6570\u636e\u63d0\u53d6\u5b8c\u6210\uff0c\u62a5\u544a\u5df2\u751f\u6210\u3002');
            setReport(data.report || '');
          } else if (data.status === 'FAILED') {
            setDetail(data.detail || '\u4efb\u52a1\u6267\u884c\u8fc7\u7a0b\u4e2d\u9047\u5230\u5f02\u5e38\u5e76\u4e2d\u65ad\u3002');
          } else {
            setDetail(data.detail || '\u6b63\u5728\u5206\u6790\u7f51\u7edc\u6570\u636e...');
          }
        } catch (error) {
          console.error('Polling error:', error);
        }
      }, 2000);
    }

    return () => {
      if (interval) clearInterval(interval);
    };
  }, [taskId, status]);

  const handleSearch = async (query: string, mode: ResearchMode) => {
    try {
      setTaskId(null);
      setStatus('PENDING');
      setDetail('\u4efb\u52a1\u5df2\u63d0\u4ea4\u81f3\u961f\u5217\uff0c\u7b49\u5f85\u5904\u7406...');
      setReport('');
      setResearchMode(mode);
      setLlmCostRmb(0);
      setExternalCostUsd(0);
      setTavilyCredits(0);
      setPublishStatus('');

      const response = await axios.post(`${API_BASE_URL}/research`, { query, research_mode: mode });
      setTaskId(response.data.task_id);
      setStatus((response.data.status || 'PENDING') as TaskStatus);
      setDetail(response.data.message || '\u4efb\u52a1\u5df2\u88ab\u63a5\u6536\uff0c\u7b49\u5f85\u5165\u961f...');
      setResearchMode((response.data.research_mode || mode) as ResearchMode);
    } catch (error) {
      console.error('Submission error:', error);
      setStatus('FAILED');
      setDetail('\u670d\u52a1\u7aef\u901a\u4fe1\u5f02\u5e38\uff0c\u8bf7\u68c0\u67e5\u7f51\u5173\u662f\u5426\u542f\u52a8\u3002');
    }
  };

  return (
    <>
      <div className="orb-bg"></div>

      <div className="app-container">
        <header style={{ marginBottom: '3rem', textAlign: 'center' }}>
          <h1 className="header-title">FactWeaver Agent</h1>
          <p className="header-subtitle">Deep Research Protocol UI v4.5</p>
        </header>

        <SearchBar
          onSearch={handleSearch}
          isLoading={status === 'PENDING' || status === 'STARTED'}
        />

        {taskId && status && (
          <StatusMonitor
            taskId={taskId}
            status={status}
            detail={detail}
            researchMode={researchMode}
            llmCostRmb={llmCostRmb}
            externalCostUsd={externalCostUsd}
            tavilyCredits={tavilyCredits}
            publishStatus={publishStatus}
          />
        )}

        {status === 'SUCCESS' && report && (
          <ReportViewer report={report} />
        )}
      </div>
    </>
  );
}

export default App;
