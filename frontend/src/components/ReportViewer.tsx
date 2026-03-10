import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface ReportViewerProps {
    report: string;
}

const ReportViewer: React.FC<ReportViewerProps> = ({ report }) => {
    if (!report) return null;

    return (
        <div className="glass-panel report-container">
            <div className="markdown-body">
                <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                >
                    {report}
                </ReactMarkdown>
            </div>
        </div>
    );
};

export default ReportViewer;
