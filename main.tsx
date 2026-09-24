import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './work';
import './styles.css';

class ErrorBoundary extends React.Component<{children:React.ReactNode},{failed:boolean}>{
  state={failed:false};
  static getDerivedStateFromError(){return {failed:true};}
  render(){return this.state.failed?<main className="fatal"><h1>Something went wrong.</h1><p>Your saved records are safe. Reload the page and try again.</p><button onClick={()=>location.reload()}>Reload</button></main>:this.props.children;}
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><ErrorBoundary><App/></ErrorBoundary></React.StrictMode>);
