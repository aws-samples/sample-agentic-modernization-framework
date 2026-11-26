import React, { useState, useRef, useEffect } from 'react';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Select from '@cloudscape-design/components/select';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Badge from '@cloudscape-design/components/badge';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { apiClient } from '../api/client';

interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  appId?: string;
}

const SUGGESTIONS = [
  "What's the migration strategy for APP-AL2023?",
  "Show me the portfolio summary",
  "What are the dependencies for APP-API?",
  "Which apps are tier 1?",
  "Update APP-DESOLA tier to tier_2",
  "What's the current workflow status?",
  "Recommend a strategy for a Java monolith with Oracle DB",
  "Run assessment for APP-ATX",
];

export const ChatPage: React.FC = () => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [appContext, setAppContext] = useState<any>({ value: 'portfolio', label: 'Portfolio (all apps)' });
  const [apps, setApps] = useState<any[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Load apps for context selector
  useEffect(() => {
    apiClient.get('/portfolio/apps').then((resp: any) => {
      const data = (resp as any)?.data || resp;
      setApps(Array.isArray(data) ? data : []);
    }).catch(() => {});
  }, []);

  // Scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const sendMessage = async (text?: string) => {
    const msg = text || input.trim();
    if (!msg) return;

    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: msg,
      timestamp: new Date().toISOString(),
      appId: appContext.value,
    };
    setMessages(prev => [...prev, userMsg]);
    setInput('');
    setLoading(true);

    try {
      const resp = await apiClient.post('/chat/message', {
        app_id: appContext.value,
        message: msg,
        session_id: `chat-${appContext.value}`,
      });

      const data = resp as any;
      const assistantContent = data?.data?.response || data?.response || 'Processing complete.';

      setMessages(prev => [...prev, {
        id: `assistant-${Date.now()}`,
        role: 'assistant',
        content: assistantContent,
        timestamp: new Date().toISOString(),
        appId: appContext.value,
      }]);
    } catch (err: any) {
      setMessages(prev => [...prev, {
        id: `error-${Date.now()}`,
        role: 'assistant',
        content: `Connection error: ${err?.message || 'Failed to reach backend'}`,
        timestamp: new Date().toISOString(),
      }]);
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const clearChat = () => {
    setMessages([]);
  };

  const appOptions = [
    { value: 'portfolio', label: 'Portfolio (all apps)' },
    ...apps.map(a => ({ value: a.app_id, label: `${a.app_name} (${a.app_id})` })),
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 120px)' }}>
      {/* Header */}
      <Header variant="h1"
        description="Chat with the Orchestrator Agent — same agent that powers Step Functions"
        actions={
          <SpaceBetween size="xs" direction="horizontal">
            <Select
              selectedOption={appContext}
              onChange={({ detail }) => setAppContext(detail.selectedOption)}
              options={appOptions}
              filteringType="auto"
              placeholder="Select app context..."
            />
            <Button iconName="remove" onClick={clearChat} disabled={messages.length === 0}>Clear</Button>
          </SpaceBetween>
        }
      >
        Agent Chat
      </Header>

      {/* Messages area */}
      <div style={{ flex: 1, overflow: 'auto', padding: '16px 0', minHeight: 0 }}>
        {messages.length === 0 ? (
          <Container>
            <SpaceBetween size="m">
              <Box textAlign="center" padding="l">
                <Box fontSize="heading-m" fontWeight="bold">Welcome to Agent Chat</Box>
                <Box color="text-body-secondary" margin={{ top: 'xs' }}>
                  I'm the Orchestrator Agent. I route your requests to specialist agents:<br/>
                  Discovery, Assessment, Planning, Design, Execution, Infrastructure.
                </Box>
              </Box>
              <Box variant="awsui-key-label">Try asking:</Box>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                {SUGGESTIONS.map((s, i) => (
                  <Button key={i} variant="normal" onClick={() => sendMessage(s)}>
                    {s}
                  </Button>
                ))}
              </div>
              <Box fontSize="body-s" color="text-body-secondary" textAlign="center">
                Context: <Badge color="blue">{appContext.label}</Badge> — Change context above to focus on a specific app.
              </Box>
            </SpaceBetween>
          </Container>
        ) : (
          <SpaceBetween size="s">
            {messages.map(msg => (
              <div key={msg.id} style={{
                display: 'flex',
                justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
                padding: '0 8px',
              }}>
                <div style={{
                  maxWidth: '75%',
                  padding: '10px 14px',
                  borderRadius: msg.role === 'user' ? '16px 16px 4px 16px' : '16px 16px 16px 4px',
                  backgroundColor: msg.role === 'user' ? '#0972d3' : '#ffffff',
                  color: msg.role === 'user' ? 'white' : '#1a1a1a',
                  border: msg.role === 'user' ? 'none' : '1px solid #e9ebed',
                  boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
                }}>
                  {msg.role === 'user' ? (
                    <div style={{ fontSize: 14, lineHeight: 1.5 }}>{msg.content}</div>
                  ) : (
                    <div style={{ fontSize: 14, lineHeight: 1.6 }}>
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{
                          p: ({ children }) => <p style={{ margin: '4px 0' }}>{children}</p>,
                          ul: ({ children }) => <ul style={{ margin: '4px 0', paddingLeft: 20 }}>{children}</ul>,
                          ol: ({ children }) => <ol style={{ margin: '4px 0', paddingLeft: 20 }}>{children}</ol>,
                          li: ({ children }) => <li style={{ margin: '2px 0' }}>{children}</li>,
                          code: ({ className, children }) => {
                            if (className) return <pre style={{ backgroundColor: '#f4f5f7', padding: 8, borderRadius: 4, overflow: 'auto', fontSize: 12 }}><code>{children}</code></pre>;
                            return <code style={{ backgroundColor: '#f0f1f3', padding: '1px 4px', borderRadius: 3, fontSize: 12 }}>{children}</code>;
                          },
                          table: ({ children }) => <table style={{ borderCollapse: 'collapse', width: '100%', margin: '8px 0', fontSize: 12 }}>{children}</table>,
                          th: ({ children }) => <th style={{ border: '1px solid #ddd', padding: '4px 8px', backgroundColor: '#f8f9fa', textAlign: 'left' }}>{children}</th>,
                          td: ({ children }) => <td style={{ border: '1px solid #ddd', padding: '4px 8px' }}>{children}</td>,
                          strong: ({ children }) => <strong>{children}</strong>,
                        }}
                      >
                        {msg.content}
                      </ReactMarkdown>
                    </div>
                  )}
                  <div style={{ fontSize: 10, opacity: 0.6, marginTop: 4, textAlign: 'right' }}>
                    {new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </div>
                </div>
              </div>
            ))}
            {loading && (
              <div style={{ padding: '0 8px' }}>
                <div style={{ display: 'inline-block', padding: '10px 14px', borderRadius: '16px 16px 16px 4px', backgroundColor: '#f8f9fa', border: '1px solid #e9ebed' }}>
                  <StatusIndicator type="loading">Agent thinking...</StatusIndicator>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </SpaceBetween>
        )}
      </div>

      {/* Input area */}
      <div style={{ padding: '12px 0', borderTop: '1px solid #e9ebed', backgroundColor: '#fafafa' }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <div style={{ flex: 1 }}>
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={`Ask the agent... (context: ${appContext.label})`}
              rows={2}
              disabled={loading}
              style={{
                width: '100%', padding: '10px 14px', border: '1px solid #d5dbdb', borderRadius: 8,
                fontSize: 14, resize: 'none', fontFamily: 'inherit', outline: 'none',
                backgroundColor: loading ? '#f8f9fa' : 'white',
              }}
            />
          </div>
          <Button variant="primary" onClick={() => sendMessage()} loading={loading} disabled={!input.trim()}>
            Send
          </Button>
        </div>
        <Box fontSize="body-s" color="text-body-secondary" margin={{ top: 'xxs' }}>
          Enter to send · Shift+Enter for new line · Agent can read/write DynamoDB, trigger assessments, generate plans
        </Box>
      </div>
    </div>
  );
};

export default ChatPage;
