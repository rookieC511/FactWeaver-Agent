import { useState, useEffect } from 'react';
import axios from 'axios';
import SearchBar from './components/SearchBar';
import type { ResearchMode } from './components/SearchBar';
import StatusMonitor from './components/StatusMonitor';
import type { TaskStatus } from './components/StatusMonitor';
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

  useEffect(() => {
    let interval: ReturnType<typeof setInterval>;

    if (taskId && (status === 'PENDING' || status === 'STARTED')) {
      interval = setInterval(async () => {
        try {
          const res = await axios.get(`${API_BASE_URL}/research/${taskId}`);
          const data = res.data;

          setStatus(data.status as TaskStatus);
          setResearchMode((data.research_mode || 'medium') as ResearchMode);
          setLlmCostRmb(Number(data.llm_cost_rmb || 0));
          setExternalCostUsd(Number(data.external_cost_usd_est || 0));
          setTavilyCredits(Number(data.tavily_credits_est || 0));

          if (data.status === 'SUCCESS') {
            setDetail('数据提取完成，报告已生成。');
            setReport(data.report || '');
          } else if (data.status === 'FAILED') {
            setDetail(data.detail || '任务执行过程中遭遇异常中断。');
          } else {
            setDetail(data.detail || '正在分析网络数据...');
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
      setDetail('任务已提交至队列，等待处理...');
      setReport('');
      setResearchMode(mode);
      setLlmCostRmb(0);
      setExternalCostUsd(0);
      setTavilyCredits(0);

      const response = await axios.post(`${API_BASE_URL}/research`, { query, research_mode: mode });
      setTaskId(response.data.task_id);
      setResearchMode((response.data.research_mode || mode) as ResearchMode);
    } catch (error) {
      console.error('Submission error:', error);
      setStatus('FAILED');
      setDetail('服务端通信异常，请检查网关是否启动。');
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
