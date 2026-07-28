import React from 'react'
import { Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Research from './pages/Research'
import Knowledge from './pages/Knowledge'
import Assistant from './pages/Assistant'
import System from './pages/System'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/research" element={<Research />} />
        <Route path="/knowledge" element={<Knowledge />} />
        <Route path="/assistant" element={<Assistant />} />
        <Route path="/system" element={<System />} />
      </Route>
    </Routes>
  )
}
