import React, { useState } from 'react';
import { Search } from 'lucide-react';

export type ResearchMode = 'low' | 'medium' | 'high';

interface SearchBarProps {
    onSearch: (query: string, researchMode: ResearchMode) => void;
    isLoading: boolean;
}

const SearchBar: React.FC<SearchBarProps> = ({ onSearch, isLoading }) => {
    const [query, setQuery] = useState('');
    const [researchMode, setResearchMode] = useState<ResearchMode>('medium');

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (query.trim() && !isLoading) {
            onSearch(query.trim(), researchMode);
        }
    };

    return (
        <form className="search-container" onSubmit={handleSubmit}>
            <input
                type="text"
                className="search-input"
                placeholder="输入你要深度研究的问题，例如：分析硅基流动 API 成本"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                disabled={isLoading}
            />
            <select
                value={researchMode}
                onChange={(e) => setResearchMode(e.target.value as ResearchMode)}
                disabled={isLoading}
                style={{
                    borderRadius: '12px',
                    padding: '0 0.9rem',
                    border: '1px solid rgba(255,255,255,0.16)',
                    background: 'rgba(255,255,255,0.06)',
                    color: 'inherit',
                    minWidth: '110px',
                }}
            >
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
            </select>
            <button type="submit" className="search-button" disabled={!query.trim() || isLoading}>
                <Search size={24} />
            </button>
        </form>
    );
};

export default SearchBar;
